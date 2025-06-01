from typing import Dict, Optional, TYPE_CHECKING, Any # Añadido Any
import pandas as pd
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import config # Para RISK_REWARD_MULTIPLIER y los flags de riesgo

if TYPE_CHECKING:
    from trade_manager import TradeManager
    # Si fetch_and_prepare_bb_data necesita Client o pytz.timezone, podrían ir aquí también
    # from binance.client import Client
    # import pytz

logger = logging.getLogger(__name__)

# --- fetch_and_prepare_bb_data ---
# Esta función se asume que está aquí y no necesita cambios para la lógica de Martingala.
# Asegúrate de que funciona como esperas, usando websocket_data_provider si es posible.
def fetch_and_prepare_bb_data(
    client: Any, # binance.client.Client
    symbol: str,
    interval_5m: str, 
    length: int,
    mult_orig: float,
    mult_new: float,
    ma_type_pine: str,
    data_limit_5m: int,
    local_tz: Any, # pytz.timezone
    verbose: bool = True,
    websocket_data_provider = None 
) -> Optional[pd.DataFrame]:
    log_prefix_local = f"bb_signal_utils.fetch_prepare ({symbol}@{interval_5m})"
    if verbose: logger.info(f"{log_prefix_local}: Iniciando preparación de datos BB (display/contexto).")
    
    df_source = None
    if websocket_data_provider:
        df_source = websocket_data_provider.get_dataframe(symbol, interval_5m)
        if df_source is None or df_source.empty: 
            logger.warning(f"{log_prefix_local}: WDP no devolvió datos para {symbol}@{interval_5m}.")
            # Podría intentar un fallback a client si WDP falla y client está disponible
            # if client: # ... lógica de fallback ...
            return None 
    elif client:
        logger.warning(f"{log_prefix_local}: WDP no provisto. Intentando obtener klines históricas con cliente API (fallback).")
        # ... (Aquí iría tu lógica para obtener klines con client.futures_klines y procesarlas) ...
        # Esta parte es un placeholder y necesitarías implementarla si este fallback es necesario.
        # Por ahora, si no hay WDP, asumimos que no se pueden obtener datos para esta función.
        logger.error(f"{log_prefix_local}: Fallback a cliente API no implementado completamente en este placeholder.")
        return None
    else:
        logger.error(f"{log_prefix_local}: Ni WDP ni cliente API provistos. No se pueden obtener datos.")
        return None

    df = df_source.copy()
    try:
        import pandas_ta as ta
        if not all(col in df.columns for col in ['open', 'high', 'low', 'close']):
            logger.error(f"{log_prefix_local}: DataFrame base no tiene columnas OHLC requeridas."); return None

        # Calcular ambas series de Bandas de Bollinger
        bb_suffix_orig = f'_{length}_{str(mult_orig).replace(".", "_")}' # Asegurar que mult_orig es string para replace
        col_names_orig = (f'BBL{bb_suffix_orig}', f'BBM{bb_suffix_orig}', f'BBU{bb_suffix_orig}', f'BBW{bb_suffix_orig}', f'BBP{bb_suffix_orig}')
        df.ta.bbands(close=df['close'], length=length, std=mult_orig, mamode=ma_type_pine.upper(), append=True, col_names=col_names_orig)
        
        bb_suffix_new = f'_{length}_{str(mult_new).replace(".", "_")}' # Asegurar que mult_new es string para replace
        col_names_new = (f'BBL{bb_suffix_new}', f'BBM{bb_suffix_new}', f'BBU{bb_suffix_new}', f'BBW{bb_suffix_new}', f'BBP{bb_suffix_new}')
        # Nota: pandas_ta podría sobreescribir BBM si los nombres de las columnas basis son iguales.
        # Si necesitas BBM_orig y BBM_new separados, asegúrate que los nombres de las columnas basis sean únicos
        # o calcula bbands en dos pasos y renombra la columna BBM intermedia.
        # Por simplicidad aquí, se asume que solo necesitas las bandas externas y el BBM de una de ellas
        # (usualmente la 'orig' es la principal para la media).
        # Si BBM_new tiene el mismo nombre que BBM_orig, el segundo cálculo de bbands lo sobreescribirá.
        # Para evitarlo, puedes darle un nombre único a BBM_new, ej. f'BBM_N{bb_suffix_new}'
        df.ta.bbands(close=df['close'], length=length, std=mult_new, mamode=ma_type_pine.upper(), append=True, col_names=col_names_new)
        
        if verbose: logger.info(f"{log_prefix_local}: Datos de {interval_5m} con BBs preparados. Shape: {df.shape}")
        return df
        
    except ImportError:
        logger.error(f"{log_prefix_local}: Librería pandas_ta no encontrada. No se pueden calcular BBs."); return None
    except Exception as e_ta:
        logger.error(f"{log_prefix_local}: Error durante el cálculo de BBs con pandas_ta: {e_ta}", exc_info=True); return None


# --- validate_and_calculate_trade_params (Ajustado para Opción B) ---
def validate_and_calculate_trade_params(
    signal_type_str: str,
    entry_price_target: Decimal,
    stop_loss_price_ref: Decimal, 
    timestamp_of_signal: pd.Timestamp,
    symbol: str,
    trade_manager: 'TradeManager',
    calculated_trade_quantity: Optional[Decimal], 
    effective_target_monetary_risk_for_sl_calc: Optional[Decimal], 
    risk_reward_multiplier: Optional[float] = None,
    verbose: bool = True # Este verbose controla los logs de ADVERTENCIA si algo falla
) -> Optional[Dict]:
    
    log_prefix_val = f"VALIDATE_PARAMS_DEBUG ({symbol} - {signal_type_str})" # Cambié el prefijo para diferenciar

    # ✅ INICIO LOGGING DE DEPURACIÓN ADICIONAL
    logger.info(f"{log_prefix_val}: === INICIO VALIDACIÓN ===")
    logger.info(f"{log_prefix_val}: Recibido Entry Target: {entry_price_target}")
    logger.info(f"{log_prefix_val}: Recibido SL Ref (será SL Actual): {stop_loss_price_ref}")
    logger.info(f"{log_prefix_val}: Recibido Calculated Qty: {calculated_trade_quantity}")
    logger.info(f"{log_prefix_val}: Recibido Target Monetary Risk: {effective_target_monetary_risk_for_sl_calc}")
    logger.info(f"{log_prefix_val}: Recibido RRR Multiplier: {risk_reward_multiplier}")
    logger.info(f"{log_prefix_val}: Recibido Verbose Flag: {verbose}")
    # ✅ FIN LOGGING DE DEPURACIÓN ADICIONAL

    if risk_reward_multiplier is None:
        risk_reward_multiplier = float(getattr(config, 'RISK_REWARD_MULTIPLIER', 10.0))
    rr_multiplier_val = Decimal(str(risk_reward_multiplier))

    actual_sl_price: Decimal = stop_loss_price_ref
    logger.info(f"{log_prefix_val}: Usando SL Precio (actual_sl_price): {actual_sl_price}")

    if not calculated_trade_quantity or calculated_trade_quantity <= Decimal(0):
        logger.error(f"{log_prefix_val}: CHECK FALLIDO: Cantidad calculada ({calculated_trade_quantity}) es inválida. Retornando None.")
        return None
    logger.info(f"{log_prefix_val}: CHECK OK: Cantidad ({calculated_trade_quantity}) válida.")

    sl_is_valid_vs_entry = False
    if signal_type_str == "COMPRA":
        if actual_sl_price < entry_price_target:
            sl_is_valid_vs_entry = True
        else:
            # El log de advertencia aquí solo se muestra si verbose es True
            if verbose: logger.warning(f"{log_prefix_val}: Detalle: SL ({actual_sl_price}) NO < Entrada ({entry_price_target}) para COMPRA.")
    elif signal_type_str == "VENTA":
        if actual_sl_price > entry_price_target:
            sl_is_valid_vs_entry = True
        else:
            if verbose: logger.warning(f"{log_prefix_val}: Detalle: SL ({actual_sl_price}) NO > Entrada ({entry_price_target}) para VENTA.")
    
    if not sl_is_valid_vs_entry:
        logger.warning(f"{log_prefix_val}: CHECK FALLIDO: SL ({actual_sl_price}) inválido vs Entrada ({entry_price_target}) para {signal_type_str}. Retornando None.")
        return None
    logger.info(f"{log_prefix_val}: CHECK OK: SL vs Entrada validado.")

    risk_per_unit = abs(entry_price_target - actual_sl_price)
    logger.info(f"{log_prefix_val}: Calculado Risk_per_unit: {risk_per_unit}")
    
    s_info_local = trade_manager.get_symbol_info_data(symbol)
    min_tick_size = s_info_local.get('tickSize', Decimal('1e-8')) if s_info_local else Decimal('1e-8')
    logger.info(f"{log_prefix_val}: Obtenido Min_tick_size: {min_tick_size}")

    if risk_per_unit < min_tick_size:
        # El log de advertencia aquí solo se muestra si verbose es True
        if verbose: logger.warning(f"{log_prefix_val}: Detalle: Riesgo por unidad ({risk_per_unit}) < tick size ({min_tick_size}).")
        logger.warning(f"{log_prefix_val}: CHECK FALLIDO: Riesgo por unidad muy pequeño. Retornando None.")
        return None
    logger.info(f"{log_prefix_val}: CHECK OK: Riesgo por unidad vs Tick size validado.")
        
    achieved_monetary_risk = risk_per_unit * calculated_trade_quantity
    logger.info(f"{log_prefix_val}: Riesgo Monetario Real Alcanzado (antes de TP): ${achieved_monetary_risk:.4f}")
    if effective_target_monetary_risk_for_sl_calc is not None and effective_target_monetary_risk_for_sl_calc > Decimal(0):
        logger.info(f"{log_prefix_val}: (Comparación) Riesgo Monetario Objetivo era: ${effective_target_monetary_risk_for_sl_calc:.4f}")

    # Calcular Take Profit
    tp_price_calculated = Decimal(0) # Inicializar
    if signal_type_str == "COMPRA":
        tp_price_calculated = entry_price_target + (rr_multiplier_val * risk_per_unit)
    elif signal_type_str == "VENTA":
        tp_price_calculated = entry_price_target - (rr_multiplier_val * risk_per_unit)
    else:
        logger.error(f"{log_prefix_val}: CHECK FALLIDO: Tipo de señal desconocido '{signal_type_str}'. Retornando None.")
        return None
    logger.info(f"{log_prefix_val}: TP calculado (antes de ajuste): {tp_price_calculated}")

    adjusted_tp_price = trade_manager.adjust_price_to_tick_size(tp_price_calculated, symbol_target=symbol)
    if adjusted_tp_price is None:
        logger.error(f"{log_prefix_val}: Fallo al ajustar precio TP. Usando TP sin ajustar: {tp_price_calculated}.")
        adjusted_tp_price = tp_price_calculated # Fallback
    logger.info(f"{log_prefix_val}: TP ajustado: {adjusted_tp_price}")

    tp_is_valid_vs_entry = False
    if signal_type_str == "COMPRA":
        if adjusted_tp_price > entry_price_target: tp_is_valid_vs_entry = True
        else: 
            if verbose: logger.warning(f"{log_prefix_val}: Detalle: TP ({adjusted_tp_price}) NO > Entrada ({entry_price_target}) para COMPRA.")
    elif signal_type_str == "VENTA":
        if adjusted_tp_price < entry_price_target: tp_is_valid_vs_entry = True
        else:
            if verbose: logger.warning(f"{log_prefix_val}: Detalle: TP ({adjusted_tp_price}) NO < Entrada ({entry_price_target}) para VENTA.")
    
    if not tp_is_valid_vs_entry:
        logger.warning(f"{log_prefix_val}: CHECK FALLIDO: TP ({adjusted_tp_price}) inválido vs Entrada ({entry_price_target}) para {signal_type_str}. Retornando None.")
        return None
    logger.info(f"{log_prefix_val}: CHECK OK: TP vs Entrada validado.")
        
    trade_params = {
        "timestamp": timestamp_of_signal,
        "entry": entry_price_target,
        "sl": actual_sl_price,
        "tp": adjusted_tp_price,
        "signal_type": signal_type_str
    }
    
    precision_fmt = trade_manager.get_price_precision(symbol)
    logger.info(f"{log_prefix_val}: === VALIDACIÓN EXITOSA ===")
    logger.info(f"{log_prefix_val}: Parámetros finales: E={entry_price_target:.{precision_fmt}f}, "
                f"SL={actual_sl_price:.{precision_fmt}f}, TP={adjusted_tp_price:.{precision_fmt}f}, "
                f"Qty={calculated_trade_quantity}")
    return trade_params
