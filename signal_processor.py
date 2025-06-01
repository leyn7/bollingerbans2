import logging
from datetime import datetime
from typing import Optional, Dict, Any
import pytz
from decimal import Decimal, InvalidOperation

from binance.client import Client

import config
from persistent_state import PersistentState
from trade_manager import TradeManager
from BB_buy import analizar_señales_compra # Asume que devuelve componentes crudos
from BB_sell import analizar_señales_venta  # Asume que devuelve componentes crudos
from bb_signal_utils import validate_and_calculate_trade_params
from websocket_data_provider import WebsocketDataProvider

logger = logging.getLogger(__name__)

LOCAL_CLIENT_INTERVAL_TO_STRING_MAP = {
    Client.KLINE_INTERVAL_1MINUTE: "1m",
    Client.KLINE_INTERVAL_5MINUTE: "5m",
    Client.KLINE_INTERVAL_15MINUTE: "15m",
}

def process_signals_and_initiate_trade(
    bot_state: PersistentState,
    trade_manager: TradeManager,
    binance_client: Client,
    local_tz: pytz.timezone,
    current_time_local: datetime,
    symbol: str,
    symbol_params: dict, # Configuración específica del símbolo
    telegram_manager=None,
    websocket_data_provider: Optional[WebsocketDataProvider] = None
):
    log_prefix = f"SIGNAL_PROC ({symbol})"
    logger.debug(f"{log_prefix}: Iniciando procesamiento de señales (Opción B: SL=BBM, Qty se calcula).")

    # Obtener configuraciones globales de config.py
    use_fixed_monetary_sl_global = getattr(config, 'USE_FIXED_MONETARY_RISK_SL', False)
    fixed_monetary_risk_base_global = Decimal(str(getattr(config, 'FIXED_MONETARY_RISK_PER_TRADE', '1.0')))
    use_percentage_risk_qty_global = getattr(config, 'USE_PERCENTAGE_RISK_MANAGEMENT', True)
    risk_percentage_for_qty_global = Decimal(str(getattr(config, 'RISK_PERCENTAGE_PER_TRADE', '0.2')))
    use_martingale_global = getattr(config, 'USE_MARTINGALE_LOSS_RECOVERY', False)
    rrr_multiplier_global = Decimal(str(getattr(config, 'RISK_REWARD_MULTIPLIER', '10.0')))

    # Parámetros para analizar_señales_compra/venta
    interval_principal_str = symbol_params.get('interval_5m', LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_5M_CFG, '5m'))
    interval_trigger_str = symbol_params.get('interval_1m', LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_1M_CFG, '1m'))
    interval_sl_ref_str = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_15M_CFG, '15m')
    ma_type = symbol_params.get('ma_type', config.MA_TYPE_CFG)
    length = int(symbol_params.get('length', config.LENGTH_CFG))
    mult_orig = float(symbol_params.get('mult_orig', config.MULT_ORIG_CFG))
    mult_new = float(symbol_params.get('mult_new', config.MULT_NEW_CFG))
    data_limit_display = int(symbol_params.get('data_limit_5m', config.DATA_LIMIT_5M_CFG))
    candles_to_print_display = int(getattr(config, 'CANDLES_TO_PRINT_CFG', 10))
    leverage = int(symbol_params.get('leverage', config.DEFAULT_LEVERAGE_FUTURES_CFG))

    s_info = trade_manager.get_symbol_info_data(symbol)
    if not s_info:
        logger.error(f"{log_prefix}: No se pudo obtener info del símbolo para {symbol}. Saltando procesamiento.")
        return
    quote_asset = s_info.get('quoteAsset', 'USDT')
    min_notional_symbol = s_info.get('minNotional', Decimal('5.0')) # Binance usualmente es 5 USDT
    min_qty_symbol = s_info.get('minQty', Decimal('0'))
    step_size_symbol = s_info.get('stepSize', Decimal('0'))


    for signal_side_config in [("BUY", f"{symbol}_LONG"), ("SELL", f"{symbol}_SHORT")]:
        signal_type_str, state_key = signal_side_config

        if bot_state.is_side_busy(state_key):
            logger.debug(f"{log_prefix}: Lado {state_key} ocupado. Saltando.")
            continue

        logger.info(f"{log_prefix}: Buscando señal cruda de {signal_type_str} para {symbol}.")
        
        # Los parámetros comunes para analizar_señales_compra/venta se definen a continuación
        # Asegúrate de pasar todos los parámetros correctos a analizar_señales_compra/venta
        # (client, symbol, intervals, trade_manager, wdp, local_tz, current_time_local, ma_params, display_params, verbose)
        # Ejemplo (debes completar con todos los parámetros):
        common_signal_params = {
            "client": binance_client, 
            "symbol": symbol,
            "interval_principal_str": interval_principal_str, 
            "interval_trigger_str": interval_trigger_str,
            "interval_sl_ref_str": interval_sl_ref_str, 
            "trade_manager": trade_manager,
            "websocket_data_provider": websocket_data_provider, 
            "local_tz": local_tz,
            "captured_time_local": current_time_local, 
            "ma_type_pine": ma_type,  # Coincide con el nombre del parámetro en BB_buy/sell
            "length": length,
            "mult_orig": mult_orig, 
            "mult_new": mult_new, 
            "data_limit_5m": data_limit_display,
            "candles_to_print_5m": candles_to_print_display,
            "verbose": getattr(config, 'VERBOSE_SIGNAL_ANALYSIS', False)
        }

        raw_signal_components: Optional[Dict[str, Any]] = None # Definir antes del if/elif

        if signal_type_str == "BUY":
            logger.debug(f"{log_prefix}: Llamando a analizar_señales_compra para {symbol} con {len(common_signal_params)} parámetros.")
            # LA LLAMADA CORRECTA ES ASÍ:
            raw_signal_components = analizar_señales_compra(**common_signal_params) 
        elif signal_type_str == "SELL":
            logger.debug(f"{log_prefix}: Llamando a analizar_señales_venta para {symbol} con {len(common_signal_params)} parámetros.")
            # LA LLAMADA CORRECTA ES ASÍ:
            raw_signal_components = analizar_señales_venta(**common_signal_params)

        if not raw_signal_components or not raw_signal_components.get("signal_detected"):
            continue

        entry_price_target = raw_signal_components['entry_price_target']
        stop_loss_price_ref = raw_signal_components['stop_loss_price_ref'] # Este es el BBM_15m
        timestamp_of_signal_utc = raw_signal_components['timestamp_of_signal_utc']

        # --- Calcular Riesgo Monetario Base (R_base_monetary_risk) ---
        R_base_monetary_risk: Optional[Decimal] = None
        if use_fixed_monetary_sl_global:
            R_base_monetary_risk = fixed_monetary_risk_base_global
        elif use_percentage_risk_qty_global:
            acc_bal = trade_manager.fetch_account_balance(asset=quote_asset)
            if acc_bal and acc_bal > Decimal(0):
                R_base_monetary_risk = acc_bal * (risk_percentage_for_qty_global / Decimal('100'))
            else:
                logger.warning(f"{log_prefix}: {signal_type_str} - No se pudo obtener balance para R_base con % de riesgo.")
                if use_martingale_global: logger.error(f"{log_prefix}: Martingala activa pero R_base no pudo ser determinado. Saltando."); continue
        else: # Fallback si ninguna de las dos config de riesgo monetario base está activa
            logger.warning(f"{log_prefix}: {signal_type_str} - No hay config para R_base monetario (ni fijo ni %).")
            if use_martingale_global: logger.error(f"{log_prefix}: Martingala activa pero no hay modo de definir R_base. Saltando."); continue
        
        if R_base_monetary_risk is None and (use_fixed_monetary_sl_global or use_martingale_global) : # Necesitamos un R_base para estos modos
            logger.error(f"{log_prefix}: {signal_type_str} - R_base es None, pero se requiere para SL monetario fijo o Martingala. Saltando.")
            continue
        if R_base_monetary_risk is not None: logger.info(f"{log_prefix}: {signal_type_str} - R_base monetario determinado: ${R_base_monetary_risk:.4f} {quote_asset}")

        # --- Calcular Riesgo Monetario Efectivo para este Trade (considerando Martingala) ---
        effective_target_monetary_risk: Optional[Decimal] = R_base_monetary_risk

        if use_martingale_global and R_base_monetary_risk is not None and R_base_monetary_risk > Decimal(0):
            accumulated_loss = bot_state.get_accumulated_loss(state_key)
            if accumulated_loss > Decimal(0):
                if rrr_multiplier_global > Decimal(0):
                    martingale_adjustment = accumulated_loss / rrr_multiplier_global
                    effective_target_monetary_risk = R_base_monetary_risk + martingale_adjustment
                    logger.info(f"{log_prefix}: {signal_type_str} - Martingala. R_base=${R_base_monetary_risk:.4f}, "
                                f"L_acum=${accumulated_loss:.4f}, Ajuste=${martingale_adjustment:.4f}. "
                                f"Riesgo Efectivo Obj=${effective_target_monetary_risk:.4f}")
                else:
                    logger.warning(f"{log_prefix}: {signal_type_str} - RISK_REWARD_MULTIPLIER ({rrr_multiplier_global}) es <=0. Usando R_base.")
        
        # --- Determinar Cantidad del Activo (calculated_trade_quantity) ---
        calculated_trade_quantity: Optional[Decimal] = None
        # El SL que se usará para calcular la cantidad (y el SL final del trade) es el stop_loss_price_ref (BBM)
        actual_sl_to_use_for_trade: Decimal = stop_loss_price_ref 

        # Modo Opción B: Calcular cantidad para cumplir effective_target_monetary_risk usando actual_sl_to_use_for_trade
        if (use_fixed_monetary_sl_global or use_martingale_global) and \
           effective_target_monetary_risk is not None and effective_target_monetary_risk > Decimal(0):
            
            logger.info(f"{log_prefix}: {signal_type_str} - Calculando Qty para Riesgo Monetario Obj. ${effective_target_monetary_risk:.4f} con SL en {actual_sl_to_use_for_trade}")
            reference_sl_distance = abs(entry_price_target - actual_sl_to_use_for_trade)
            min_tick = s_info.get('tickSize', Decimal('1e-8'))

            if reference_sl_distance >= min_tick:
                ideal_asset_quantity = effective_target_monetary_risk / reference_sl_distance
                calculated_trade_quantity = ideal_asset_quantity
                logger.info(f"{log_prefix}: {signal_type_str} - Cantidad Ideal (antes de ajustes): {ideal_asset_quantity:.8f}")
            else:
                logger.warning(f"{log_prefix}: {signal_type_str} - Distancia SL de referencia ({reference_sl_distance}) es < tickSize ({min_tick}) o cero. No se puede calcular cantidad para riesgo monetario.")
        
        # Si no se calculó cantidad arriba (porque no es modo monetario SL o falló), o si ese modo no está activo,
        # usar lógica de cantidad por % de riesgo del capital (si está activo) o fixed_quantity de symbol_params.
        elif use_percentage_risk_qty_global: # (y use_fixed_monetary_sl_global es False, y Martingala es False o R_base era None)
            if R_base_monetary_risk is not None and R_base_monetary_risk > Decimal(0): # R_base_monetary_risk aquí es el de % de balance
                reference_sl_distance = abs(entry_price_target - actual_sl_to_use_for_trade) # SL es BBM
                min_tick = s_info.get('tickSize', Decimal('1e-8'))
                if reference_sl_distance >= min_tick:
                    ideal_qty = R_base_monetary_risk / reference_sl_distance
                    calculated_trade_quantity = ideal_qty
                    logger.info(f"{log_prefix}: {signal_type_str} - Cantidad (modo % riesgo qty global): {calculated_trade_quantity:.8f} (usando R_base: ${R_base_monetary_risk})")
                else:
                    logger.warning(f"{log_prefix}: {signal_type_str} - Distancia SL de referencia ({reference_sl_distance}) muy pequeña para cantidad por % riesgo.")
            else:
                logger.warning(f"{log_prefix}: {signal_type_str} - No se pudo calcular R_base monetario para cantidad por % de riesgo.")
        
        # Fallback final a fixed_quantity de symbol_params si quantity aún no está determinada
        if calculated_trade_quantity is None:
            fixed_qty_asset_str = symbol_params.get('fixed_quantity', str(getattr(config, 'DEFAULT_FIXED_TRADE_QUANTITY', '0')))
            try:
                qty_val = Decimal(str(fixed_qty_asset_str))
                if qty_val > Decimal(0):
                    calculated_trade_quantity = qty_val
                    logger.info(f"{log_prefix}: {signal_type_str} - Cantidad (fallback a fixed_quantity de symbol_params/default): {calculated_trade_quantity}")
                else:
                    logger.error(f"{log_prefix}: {signal_type_str} - fixed_quantity de fallback '{fixed_qty_asset_str}' no es positivo.")
            except InvalidOperation:
                logger.error(f"{log_prefix}: {signal_type_str} - fixed_quantity de fallback '{fixed_qty_asset_str}' no es Decimal válido.")

        # --- Validar y Ajustar Cantidad Calculada ---
        if not calculated_trade_quantity or calculated_trade_quantity <= Decimal(0):
            logger.warning(f"{log_prefix}: {signal_type_str} - Cantidad calculada es inválida o cero ({calculated_trade_quantity}). No se opera.")
            continue
            
        final_adjusted_quantity = trade_manager.adjust_quantity_to_step_size(calculated_trade_quantity, symbol)
        if not final_adjusted_quantity or final_adjusted_quantity <= Decimal(0):
            logger.warning(f"{log_prefix}: {signal_type_str} - Cantidad ajustada a step_size es inválida ({final_adjusted_quantity}). No se opera.")
            continue

        if final_adjusted_quantity < min_qty_symbol:
            logger.warning(f"{log_prefix}: {signal_type_str} - Cantidad ajustada {final_adjusted_quantity} < minQty ({min_qty_symbol}). NO SE OPERA para mantener el riesgo.")
            continue # No operar si la cantidad es demasiado pequeña para el riesgo deseado

        notional_value = final_adjusted_quantity * entry_price_target
        if notional_value < min_notional_symbol:
            logger.warning(f"{log_prefix}: {signal_type_str} - Valor nocional {notional_value:.4f} (Qty:{final_adjusted_quantity} * EP:{entry_price_target}) "
                           f"es menor que minNotional ({min_notional_symbol}). NO SE OPERA.")
            continue
        
        logger.info(f"{log_prefix}: {signal_type_str} - Cantidad final para trade: {final_adjusted_quantity}. Nocional: {notional_value:.2f}")

        # --- PASO 5: Validar parámetros finales y obtener SL/TP ---
        # validate_and_calculate_trade_params usará effective_target_monetary_risk_for_sl_calc
        # para determinar el SL si ese riesgo está definido y es aplicable (según flags de config).
        # Si effective_target_monetary_risk_for_sl_calc es None o no aplica, usará stop_loss_price_ref.
        validated_trade_params = validate_and_calculate_trade_params(
            signal_type_str=signal_type_str,
            entry_price_target=entry_price_target,
            stop_loss_price_ref=actual_sl_to_use_for_trade, # Este es el SL que se usará (BBM_15m)
            timestamp_of_signal=timestamp_of_signal_utc,
            symbol=symbol,
            trade_manager=trade_manager,
            calculated_trade_quantity=final_adjusted_quantity,
            # Este es el riesgo monetario que se INTENTÓ alcanzar con la cantidad calculada y el SL de referencia.
            # validate_and_calculate_trade_params lo usará para logging/verificación.
            effective_target_monetary_risk_for_sl_calc=effective_target_monetary_risk, 
            risk_reward_multiplier=float(rrr_multiplier_global),
            verbose=getattr(config, 'VERBOSE_SIGNAL_ANALYSIS', True)
        )

        if validated_trade_params:
            logger.info(f"{log_prefix}: SEÑAL {signal_type_str} VALIDADA para {symbol}. "
                        f"E={validated_trade_params['entry']:.{trade_manager.get_price_precision(symbol)}f}, "
                        f"SL={validated_trade_params['sl']:.{trade_manager.get_price_precision(symbol)}f}, "
                        f"TP={validated_trade_params['tp']:.{trade_manager.get_price_precision(symbol)}f}, "
                        f"Qty={final_adjusted_quantity}")
            
            gate_bbl_5m_str, gate_bbu_5m_str = str(None), str(None)
            pre_bbl_orig_5m_str, pre_bbu_orig_5m_str, gating_bbm_orig_5m_str = str(None), str(None), str(None)

            if websocket_data_provider: # Obtener bandas actuales para el estado
                bands_5m_ctx = websocket_data_provider.get_contextual_bollinger_bands(symbol, interval_principal_str)
                if bands_5m_ctx:
                    gate_bbl_5m_str = str(bands_5m_ctx.get('BBL_new'))
                    gate_bbu_5m_str = str(bands_5m_ctx.get('BBU_new'))
                    pre_bbl_orig_5m_str = str(bands_5m_ctx.get('BBL_orig'))
                    pre_bbu_orig_5m_str = str(bands_5m_ctx.get('BBU_orig'))
                    gating_bbm_orig_5m_str = str(bands_5m_ctx.get('BBM_orig'))
            
            initial_trade_details = {
                "status": "PENDING_DYNAMIC_LIMIT", "symbol": symbol, 
                "signal_type": signal_type_str, 
                "position_side_hedge_mode": "LONG" if signal_type_str == "BUY" else "SHORT",
                "target_entry_price": str(validated_trade_params['entry']), 
                "target_sl_price": str(validated_trade_params['sl']), 
                "target_tp_price": str(validated_trade_params['tp']), 
                "quantity": str(final_adjusted_quantity),
                "leverage": leverage, 
                "timestamp_signal_iso": validated_trade_params['timestamp'].isoformat(),
                "last_1m_update_timestamp_iso": current_time_local.astimezone(pytz.utc).isoformat(),
                "last_5m_bb_update_timestamp_iso": current_time_local.astimezone(pytz.utc).isoformat(),
                "current_entry_order_id": None, "entry_order_response_initial": None,
                "gate_band_5m_lower_str": gate_bbl_5m_str, 
                "gate_band_5m_upper_str": gate_bbu_5m_str,
                "pre_check_bbl_orig_primary_str": pre_bbl_orig_5m_str, 
                "pre_check_bbu_orig_primary_str": pre_bbu_orig_5m_str,
                "pre_check_bbm_15m_str": str(stop_loss_price_ref),
                "gating_bbm_orig_5m_str": gating_bbm_orig_5m_str,
                "target_monetary_risk_trade": str(effective_target_monetary_risk) if effective_target_monetary_risk is not None else str(R_base_monetary_risk),
                "accumulated_loss_at_entry": str(bot_state.get_accumulated_loss(state_key))
            }
            bot_state.set_active_trade(state_key, initial_trade_details)
            logger.info(f"{log_prefix}: Trade de {signal_type_str} ({state_key}) iniciado en PENDING_DYNAMIC_LIMIT.")
            # (Enviar notificación a Telegram si es necesario)
        else:
            logger.info(f"{log_prefix}: Señal {signal_type_str} para {symbol} no pasó la validación final de parámetros.")
            
    logger.debug(f"{log_prefix}: Fin de procesamiento de señales para {symbol}.")