# Archivo: pending_order_manager.py
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
import pytz

from binance.client import Client # Para constantes como Client.SIDE_BUY/SELL y Client.KLINE_INTERVAL_1MINUTE

import config
from bb_utils import get_latest_5m_bollinger_bands_data
from persistent_state import PersistentState
from signal_processor import LOCAL_CLIENT_INTERVAL_TO_STRING_MAP
from trade_manager import TradeManager
from websocket_data_provider import WebsocketDataProvider # Para type hinting y uso


logger = logging.getLogger(__name__)

def _get_decimal_from_bb_data(bb_data_dict, key, log_prefix_func, default_on_error=None):
    """Obtiene y convierte un valor de un diccionario de datos BB a Decimal de forma segura."""
    val_str = bb_data_dict.get(key)
    if val_str is None:
        logger.warning(f"{log_prefix_func}: Clave '{key}' no encontrada en datos BB.")
        return default_on_error
    try:
        # Los valores de get_latest_5m_bollinger_bands_data ya son strings de Decimales.
        return Decimal(str(val_str))
    except (InvalidOperation, TypeError, ValueError) as e:
        logger.error(f"{log_prefix_func}: Error convirtiendo '{key}' ('{val_str}') a Decimal: {e}")
        return default_on_error

def _handle_emergency_close(
    trade_details_for_side: dict,
    side_being_managed: str,
    bot_state: PersistentState,
    trade_manager: TradeManager,
    failure_reason_prefix: str
) -> tuple[dict | None, bool]:
    """Función auxiliar para cerrar una posición a mercado en caso de fallo crítico."""
    logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - EMERGENCY_CLOSE): {failure_reason_prefix}. ¡CERRANDO POSICIÓN INMEDIATAMENTE A MERCADO!")
    
    quantity_to_close = Decimal(trade_details_for_side['quantity'])
    signal_type_to_close = trade_details_for_side['signal_type'] # "BUY" o "SELL"
    
    market_close_side = Client.SIDE_SELL if signal_type_to_close == "BUY" else Client.SIDE_BUY
    position_side_config_to_close = trade_details_for_side.get("position_side_hedge_mode") # "LONG" o "SHORT"

    close_order_response = trade_manager.place_market_order(
        side=market_close_side,
        quantity=quantity_to_close,
        reduce_only=True, 
        position_side_for_hedge=position_side_config_to_close
    )

    final_reason = failure_reason_prefix
    if close_order_response and close_order_response.get('orderId'):
        logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - EMERGENCY_CLOSE): Posición cerrada a mercado. Orden de cierre ID: {close_order_response.get('orderId')}")
        final_reason += f", orden de cierre {close_order_response.get('orderId')}"
    else:
        logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - EMERGENCY_CLOSE): ¡FALLO CRÍTICO al intentar cerrar posición! Resp: {close_order_response}")
        final_reason += ", intento de cierre a mercado falló"
    
    bot_state.clear_active_trade(side_being_managed, reason=final_reason)
    return bot_state.get_active_trade(side_being_managed), True # Indicar que se debe salir


def _process_filled_order(
    trade_details_for_side: dict,
    side_being_managed: str,
    entry_order_info: dict, # Información de la orden de entrada que se llenó
    bot_state: PersistentState,
    trade_manager: TradeManager,
    log_prefix: str # Ej: "5M_UPD_FILLED" o "FILL_CHECK"
) -> tuple[dict | None, bool]:
    """
    Lógica común para procesar una orden de entrada que se ha llenado.
    Coloca SL/TP, o cierra si hay problemas. Actualiza el estado.
    Devuelve (trade_details_actualizado, debe_salir_temprano_de_gestion_pendiente)
    """
    logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): ¡ORDEN {entry_order_info.get('orderId')} LLENADA!")

    # 1. Calcular precio de entrada real de forma robusta
    avg_price_str = entry_order_info.get('avgPrice')
    calculated_actual_entry_price = Decimal(0)

    if avg_price_str:
        try:
            price_from_avg = Decimal(avg_price_str)
            if price_from_avg > 0:
                calculated_actual_entry_price = price_from_avg
            else:
                logger.warning(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): avgPrice ('{avg_price_str}') es cero o negativo. Usando target_entry_price.")
        except Exception as e_dec:
            logger.warning(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): No se pudo convertir avgPrice ('{avg_price_str}') a Decimal: {e_dec}. Usando target_entry_price.")

    if calculated_actual_entry_price <= 0:
        logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Usando target_entry_price como fallback para actual_entry_price.")
        try:
            calculated_actual_entry_price = Decimal(trade_details_for_side['target_entry_price'])
            if calculated_actual_entry_price <= 0:
                logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Fallback target_entry_price ('{trade_details_for_side['target_entry_price']}') también inválido.")
        except Exception as e_dec_target:
            logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): No se pudo convertir target_entry_price a Decimal: {e_dec_target}")
            calculated_actual_entry_price = Decimal(0)
    
    actual_entry_price = calculated_actual_entry_price

    if actual_entry_price <= 0:
        return _handle_emergency_close(trade_details_for_side, side_being_managed, bot_state, trade_manager,
                                       f"Precio de entrada inválido ({actual_entry_price}) después de FILL")

    # --- Variables para SL/TP ---
    target_sl_price_str = trade_details_for_side['target_sl_price']
    target_tp_price_str = trade_details_for_side['target_tp_price']
    quantity = Decimal(trade_details_for_side['quantity'])
    signal_type = trade_details_for_side['signal_type'] # "BUY" o "SELL" (de la señal original)
    # El lado del activo en la posición para enviar a trade_manager.place_stop_loss/take_profit_order
    position_asset_side_for_sl_tp = Client.SIDE_BUY if signal_type == "BUY" else Client.SIDE_SELL

    sl_order_response_placed = None
    tp_order_response_placed = None
    sl_placement_successful = False

    # 2. INTENTAR VALIDAR Y COLOCAR STOP LOSS
    if target_sl_price_str:
        sl_price_decimal = Decimal(target_sl_price_str)
        sl_is_valid_vs_entry = (signal_type == "BUY" and sl_price_decimal < actual_entry_price) or \
                               (signal_type == "SELL" and sl_price_decimal > actual_entry_price)

        if sl_is_valid_vs_entry:
            current_market_price = trade_manager.get_current_market_price()
            if current_market_price:
                sl_is_safe_to_place_market = (signal_type == "BUY" and current_market_price > sl_price_decimal) or \
                                             (signal_type == "SELL" and current_market_price < sl_price_decimal)
                if sl_is_safe_to_place_market:
                    logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): SL válido ({sl_price_decimal}) vs Mercado ({current_market_price}). Intentando colocar SL.")
                    sl_order_response_placed = trade_manager.place_stop_loss_order(
                        position_asset_side=position_asset_side_for_sl_tp,
                        sl_price=sl_price_decimal
                        # Ya no se pasa 'quantity'
                        # position_side_for_hedge y client_order_id son opcionales en la definición de place_stop_loss_order,
                        # así que solo se pasan si los tienes y los quieres usar.
                        # Tu trade_manager.place_stop_loss_order ya usa self.symbol,
                        # pero si la hiciste más genérica para aceptar symbol_target, lo pasarías aquí.
                    )
                    if sl_order_response_placed and sl_order_response_placed.get('orderId'):
                        logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): SL colocado. ID: {sl_order_response_placed.get('orderId')}")
                        sl_placement_successful = True
                    else:
                        logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Fallo al colocar orden SL. Respuesta: {sl_order_response_placed}")
                else:
                    logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): SL ({sl_price_decimal}) NO SE PUEDE COLOCAR. Precio de mercado ({current_market_price}) ya ha superado el SL para {signal_type}.")
            else:
                logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): No se pudo obtener el precio de mercado para validar SL. SL no colocado.")
        else:
            logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Precio de SL ({sl_price_decimal}) inválido vs entrada ({actual_entry_price}) para {signal_type}. SL no colocado.")
    else:
        logger.warning(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): No hay target_sl_price_str. SL no colocado.")
        # Si el SL es mandatorio, aquí también se podría considerar un fallo crítico. Por ahora, solo advertimos.
        # Si se quiere que sea mandatorio, sl_placement_successful no se pondría a True.

    # 3. SI EL SL NO FUE COLOCADO CON ÉXITO (Y ES MANDATORIO), CERRAR POSICIÓN
    # Asumimos que el SL es mandatorio. Si no hay target_sl_price_str o falla la colocación.
    if not sl_placement_successful: # Esta condición cubre todos los fallos de SL anteriores
        return _handle_emergency_close(trade_details_for_side, side_being_managed, bot_state, trade_manager,
                                       "Fallo en colocación de SL o SL no definido")

    # 4. SI EL SL FUE EXITOSO, INTENTAR COLOCAR TAKE PROFIT
    if target_tp_price_str:
        tp_price_decimal = Decimal(target_tp_price_str)
        tp_is_valid_vs_entry = (signal_type == "BUY" and tp_price_decimal > actual_entry_price) or \
                               (signal_type == "SELL" and tp_price_decimal < actual_entry_price)
        if tp_is_valid_vs_entry:
            logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Intentando colocar orden TP para {signal_type} en {tp_price_decimal}")
            tp_order_response_placed = trade_manager.place_take_profit_order(
                        position_asset_side=position_asset_side_for_sl_tp,
                        tp_price=tp_price_decimal
                        # Ya no se pasa 'quantity'
                    )
            if tp_order_response_placed and tp_order_response_placed.get('orderId'):
                 logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): TP colocado. ID: {tp_order_response_placed.get('orderId')}")
            else:
                 logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Fallo al colocar orden TP. Respuesta: {tp_order_response_placed}")
        else:
            logger.error(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Precio de TP ({tp_price_decimal}) inválido vs entrada ({actual_entry_price}) para {signal_type}. TP no colocado.")
    else:
        logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): No hay target_tp_price_str. TP no colocado.")

    # 5. ACTUALIZAR ESTADO A POSITION_OPEN
    final_trade_details = {
        "status": "POSITION_OPEN", "symbol": trade_details_for_side['symbol'],
        "side": signal_type, # El 'side' de la POSICION es el 'signal_type' original
        "quantity": trade_details_for_side['quantity'],
        "entry_price_actual": str(actual_entry_price),
        "entry_order_response": entry_order_info, 
        "sl_order_response": sl_order_response_placed, 
        "tp_order_response": tp_order_response_placed, 
        "position_side_hedge_mode": trade_details_for_side.get('position_side_hedge_mode')
    }
    bot_state.set_active_trade(side_being_managed, final_trade_details)
    logger.info(f"PENDING_ORDER_MGR ({side_being_managed} - {log_prefix}): Trade transicionado a POSITION_OPEN.")
    return bot_state.get_active_trade(side_being_managed), True # Indicar que se debe salir de la gestión pendiente


# --- Sección A: Lógica de Actualización de 5 Minutos ---
def _perform_5_minute_updates(
    trade_details_for_side: dict, 
    side_being_managed: str,     
    bot_state: PersistentState, # Cambiado de 'object' a tipo específico
    trade_manager: TradeManager, # Cambiado de 'object' a tipo específico    
    current_time_local: datetime, 
    current_time_utc: datetime,     
    symbol: str,                 
    symbol_params: dict,
    websocket_data_provider # <--- NUEVO PARÁMETRO
) -> tuple[dict | None, bool]:
    
    current_entry_order_id_from_state = trade_details_for_side.get('current_entry_order_id')
    log_prefix_5m = f"PENDING_ORDER_MGR ({symbol} - {side_being_managed} - 5M_UPD)"
    logger.debug(f"{log_prefix_5m}: Iniciando actualizaciones de TF principal. Orden ID: {current_entry_order_id_from_state or 'N/A'}")
    
    if 'last_5m_bb_update_timestamp_iso' not in trade_details_for_side or \
       not trade_details_for_side['last_5m_bb_update_timestamp_iso']:
        logger.error(f"{log_prefix_5m}: Falta 'last_5m_bb_update_timestamp_iso'. No se puede actualizar.")
        return trade_details_for_side, False 

    # Parámetros BB
    interval_principal_str = symbol_params.get('interval_5m', config.INTERVAL_5M_CFG if hasattr(config, 'INTERVAL_5M_CFG') else '5m')
    ma_type_principal = symbol_params.get('ma_type', config.MA_TYPE_CFG)
    length_principal = int(symbol_params.get('length', config.LENGTH_CFG))
    mult_orig_principal = float(symbol_params.get('mult_orig', config.MULT_ORIG_CFG))
    mult_new_principal = float(symbol_params.get('mult_new', config.MULT_NEW_CFG))
    
    sl_bb_interval_str = config.INTERVAL_15M_CFG 
    sl_bb_ma_type = symbol_params.get('ma_type_sl_15m', ma_type_principal) 
    sl_bb_length = int(symbol_params.get('length_sl_15m', length_principal))
    sl_bb_mult_orig = float(symbol_params.get('mult_orig_sl_15m', mult_orig_principal)) 
    sl_bb_mult_new = float(symbol_params.get('mult_new_sl_15m', mult_new_principal))

    if not websocket_data_provider:
        logger.error(f"{log_prefix_5m}: WebsocketDataProvider no proporcionado. No se pueden obtener datos BB.")
        return trade_details_for_side, False

    # 1. Obtener datos BB frescos del timeframe principal (ej. 5m)
    new_primary_bb_data = get_latest_5m_bollinger_bands_data(        
        symbol=symbol, interval_5m_str=interval_principal_str,
        ma_type_pine=ma_type_principal, length=length_principal,
        mult_orig=mult_orig_principal, mult_new=mult_new_principal,
        websocket_data_provider=websocket_data_provider # <--- AÑADIDO
    )
    
    # 2. Obtener datos BB frescos de 15m para el SL dinámico y la pre-condición
    data_15m_for_sl_and_precheck = get_latest_5m_bollinger_bands_data(       
        symbol=symbol, interval_5m_str=sl_bb_interval_str, # Usar string "15m"
        ma_type_pine=sl_bb_ma_type, length=sl_bb_length,
        mult_orig=sl_bb_mult_orig, mult_new=sl_bb_mult_new,
        websocket_data_provider=websocket_data_provider # <--- AÑADIDO
    )

    trade_details_for_side['last_5m_bb_update_timestamp_iso'] = current_time_utc.isoformat() # Actualizar timestamp primero

    if new_primary_bb_data.get("error") or data_15m_for_sl_and_precheck.get("error"):
        logger.error(f"{log_prefix_5m}: No se pudieron obtener todos los datos BB necesarios. "
                     f"5m error: {new_primary_bb_data.get('message', 'OK' if not new_primary_bb_data.get('error') else 'Error')}, "
                     f"15m error: {data_15m_for_sl_and_precheck.get('message', 'OK' if not data_15m_for_sl_and_precheck.get('error') else 'Error')}")
        bot_state.set_active_trade(side_being_managed, trade_details_for_side)
        return trade_details_for_side, False
        
    logger.info(f"{log_prefix_5m}: Datos BB ({interval_principal_str}) OK. Datos BB ({sl_bb_interval_str}) para SL/PreCheck OK.")

    # --- INICIO: Almacenar valores para la PRE-CONDICIÓN en _perform_1_minute_gating ---
    # Estos se actualizan siempre si los datos se obtienen con éxito.
    # Valor de BBL_orig del timeframe principal
    bbl_orig_ptf_for_precheck = _get_decimal_from_bb_data(new_primary_bb_data, 'BBL_orig', log_prefix_5m)
    if bbl_orig_ptf_for_precheck is not None:
        adj_val = trade_manager.adjust_price_to_tick_size(bbl_orig_ptf_for_precheck, symbol_target=symbol)
        trade_details_for_side['pre_check_bbl_orig_primary_str'] = str(adj_val) if adj_val is not None else None
    else:
        trade_details_for_side['pre_check_bbl_orig_primary_str'] = None
        logger.warning(f"{log_prefix_5m}: No se pudo obtener BBL_orig_{interval_principal_str} para pre-condición.")

    # Valor de BBU_orig del timeframe principal
    bbu_orig_ptf_for_precheck = _get_decimal_from_bb_data(new_primary_bb_data, 'BBU_orig', log_prefix_5m)
    if bbu_orig_ptf_for_precheck is not None:
        adj_val = trade_manager.adjust_price_to_tick_size(bbu_orig_ptf_for_precheck, symbol_target=symbol)
        trade_details_for_side['pre_check_bbu_orig_primary_str'] = str(adj_val) if adj_val is not None else None
    else:
        trade_details_for_side['pre_check_bbu_orig_primary_str'] = None
        logger.warning(f"{log_prefix_5m}: No se pudo obtener BBU_orig_{interval_principal_str} para pre-condición.")

    bbm_15m_val = _get_decimal_from_bb_data(data_15m_for_sl_and_precheck, 'BBM_orig', log_prefix_5m)
    if bbm_15m_val is not None: trade_details_for_side['pre_check_bbm_15m_str'] = str(trade_manager.adjust_price_to_tick_size(bbm_15m_val, symbol))
    else: logger.warning(f"{log_prefix_5m}: No BBM_{sl_bb_interval_str} para pre-condición/SL.")

    new_dynamic_sl_price = bbm_15m_val
    effective_sl_price = None
    if new_dynamic_sl_price is not None: effective_sl_price = new_dynamic_sl_price
    else:
        logger.warning(f"{log_prefix_5m}: No se actualizó SL dinámico (BBM_{sl_bb_interval_str}). Usando SL existente."); 
        try: effective_sl_price = Decimal(trade_details_for_side['target_sl_price'])
        except: logger.error(f"{log_prefix_5m}: 'target_sl_price' existente inválido."); bot_state.set_active_trade(side_being_managed, trade_details_for_side); return trade_details_for_side, False
    
    new_target_entry_dec = None
    if trade_details_for_side['signal_type'] == 'BUY': new_target_entry_dec = _get_decimal_from_bb_data(new_primary_bb_data, 'BBL_new', log_prefix_5m)
    elif trade_details_for_side['signal_type'] == 'SELL': new_target_entry_dec = _get_decimal_from_bb_data(new_primary_bb_data, 'BBU_new', log_prefix_5m)

    if new_target_entry_dec is not None:
        adj_new_target_entry = trade_manager.adjust_price_to_tick_size(new_target_entry_dec, symbol)
        adj_effective_sl = trade_manager.adjust_price_to_tick_size(effective_sl_price, symbol)
        if adj_new_target_entry and adj_effective_sl:
            valid_vs_sl = (trade_details_for_side['signal_type']=="BUY" and adj_new_target_entry > adj_effective_sl) or \
                          (trade_details_for_side['signal_type']=="SELL" and adj_new_target_entry < adj_effective_sl)
            if valid_vs_sl:
                risk = abs(adj_new_target_entry - adj_effective_sl)
                if risk > Decimal('1e-9'):
                    rr_mult = Decimal(str(config.RISK_REWARD_MULTIPLIER))
                    new_tp_dec = adj_new_target_entry + (rr_mult * risk) if trade_details_for_side['signal_type'] == "BUY" else adj_new_target_entry - (rr_mult * risk)
                    adj_new_tp = trade_manager.adjust_price_to_tick_size(new_tp_dec, symbol)
                    if adj_new_tp:
                        trade_details_for_side['target_entry_price'] = str(adj_new_target_entry)
                        trade_details_for_side['target_sl_price'] = str(adj_effective_sl)
                        trade_details_for_side['target_tp_price'] = str(adj_new_tp)
                        logger.info(f"{log_prefix_5m}: Precios de trade actualizados: E={str(adj_new_target_entry)}, SL={str(adj_effective_sl)}, TP={str(adj_new_tp)}")
                else: logger.warning(f"{log_prefix_5m}: Riesgo con nuevo entry y SL es muy pequeño. No se actualizan precios.")
            else: logger.warning(f"{log_prefix_5m}: Nuevo target_entry ({adj_new_target_entry}) inválido vs SL ({adj_effective_sl}). No se actualizan precios.")
    else: logger.error(f"{log_prefix_5m}: No se pudo determinar nuevo target_entry_price. No se actualizan precios.")
    
    gate_lower = _get_decimal_from_bb_data(new_primary_bb_data, 'BBL_new', log_prefix_5m)
    gate_upper = _get_decimal_from_bb_data(new_primary_bb_data, 'BBU_new', log_prefix_5m)
    if gate_lower and gate_upper:
        adj_gl = trade_manager.adjust_price_to_tick_size(gate_lower, symbol); adj_gu = trade_manager.adjust_price_to_tick_size(gate_upper, symbol)
        if adj_gl and adj_gu:
            trade_details_for_side['gate_band_5m_lower_str'] = str(adj_gl); trade_details_for_side['gate_band_5m_upper_str'] = str(adj_gu)
            logger.debug(f"{log_prefix_5m}: Bandas Gate ({interval_principal_str}) actualizadas: L={str(adj_gl)}, U={str(adj_gu)}")

    if current_entry_order_id_from_state and 'target_entry_price' in trade_details_for_side:
        pos_side_hedge = trade_details_for_side.get("position_side_hedge_mode")
        curr_order_info = trade_manager.check_order_status(order_id=current_entry_order_id_from_state, position_side_for_hedge=pos_side_hedge, symbol_target=symbol)
        if curr_order_info:
            order_status = curr_order_info.get('status')
            if order_status in ['NEW', 'PARTIALLY_FILLED']:
                price_on_exch_str = curr_order_info.get('price', '0');
                try:
                    target_entry_state_dec = Decimal(trade_details_for_side['target_entry_price']); price_exch_dec = Decimal(price_on_exch_str)
                    if target_entry_state_dec != price_exch_dec:
                        logger.info(f"{log_prefix_5m}: Precio objetivo ({target_entry_state_dec}) difiere de exchange ({price_exch_dec}). Reemplazando orden {current_entry_order_id_from_state}.")
                        if trade_manager.cancel_order_if_open(order_id=current_entry_order_id_from_state, position_side_for_hedge=pos_side_hedge, symbol_target=symbol):
                            trade_details_for_side['current_entry_order_id'] = None; trade_details_for_side['entry_order_response_initial'] = None
                            logger.info(f"{log_prefix_5m}: Orden {current_entry_order_id_from_state} cancelada. Se intentará recolocar.")
                        else: logger.warning(f"{log_prefix_5m}: No se pudo cancelar orden {current_entry_order_id_from_state} para reemplazar.")
                except InvalidOperation: logger.error(f"{log_prefix_5m}: Error convirtiendo precios a Decimal para reemplazo.")
            elif order_status == "FILLED":
                logger.info(f"{log_prefix_5m}: Orden {current_entry_order_id_from_state} LLENADA durante 5m_updates."); bot_state.set_active_trade(side_being_managed, trade_details_for_side)
                return _process_filled_order(trade_details_for_side, side_being_managed, curr_order_info, bot_state, trade_manager, f"5M_UPD_FILLED ({symbol})")
            elif order_status not in ['ERROR_API_CHECK', 'ERROR_UNEXPECTED_CHECK']: trade_details_for_side['current_entry_order_id'] = None; trade_details_for_side['entry_order_response_initial'] = None # Orden ya no es válida
        else: trade_details_for_side['current_entry_order_id'] = None; trade_details_for_side['entry_order_response_initial'] = None # Error al chequear
    
    bot_state.set_active_trade(side_being_managed, trade_details_for_side)
    return bot_state.get_active_trade(side_being_managed), False


# --- Sección B: Lógica de Gating de Órdenes de 1 Minuto ---
# (Función _perform_1_minute_gating sin cambios, omitida por brevedad)
def _perform_1_minute_gating(
    trade_details_for_side: dict,
    side_being_managed: str,
    bot_state: PersistentState, 
    trade_manager: TradeManager, 
    # client: Client, # Ya no se usa para klines de 1m si usamos WDP
    websocket_data_provider: WebsocketDataProvider, # <--- NUEVO para obtener DF de 1m
    position_side_param: dict, # Para place_limit_entry_order
    symbol: str,
    interval_trigger_str: str = "1m" # El intervalo para el precio actual
) -> dict: # Devuelve los trade_details actualizados (especialmente current_entry_order_id)
    
    current_entry_order_id = trade_details_for_side.get('current_entry_order_id')
    log_prefix_1m_gate = f"PENDING_1M_GATING ({symbol} - {side_being_managed})"
    logger.debug(f"{log_prefix_1m_gate}: Iniciando NUEVA lógica de gating. Orden ID actual: {current_entry_order_id or 'N/A'}")

    # Obtener datos necesarios de trade_details (ya deberían estar como strings)
    try:
        signal_type = trade_details_for_side['signal_type']
        quantity_str = trade_details_for_side['quantity']
        target_entry_price_str = trade_details_for_side['target_entry_price'] # BBL_new_5m o BBU_new_5m
        
        # Bandas para la zona de gating
        bbm_orig_5m_str = trade_details_for_side.get('gating_bbm_orig_5m_str')
        bbl_orig_5m_str = trade_details_for_side.get('pre_check_bbl_orig_primary_str')
        bbu_orig_5m_str = trade_details_for_side.get('pre_check_bbu_orig_primary_str')

        if not all([quantity_str, target_entry_price_str, bbm_orig_5m_str, bbl_orig_5m_str if signal_type == "BUY" else True, bbu_orig_5m_str if signal_type == "SELL" else True]):
            logger.warning(f"{log_prefix_1m_gate}: Faltan datos esenciales de bandas o trade en trade_details para gating. Omitiendo. Revise signal_processor.py.")
            return trade_details_for_side
        
        quantity = Decimal(quantity_str)
        target_entry_price_decimal = Decimal(target_entry_price_str)
        gating_BBM_orig_5m = Decimal(bbm_orig_5m_str)
        gating_BBL_orig_5m = Decimal(bbl_orig_5m_str) if signal_type == "BUY" else None # Solo necesario para BUY
        gating_BBU_orig_5m = Decimal(bbu_orig_5m_str) if signal_type == "SELL" else None # Solo necesario para SELL

    except (KeyError, InvalidOperation, TypeError) as e:
        logger.error(f"{log_prefix_1m_gate}: Error convirtiendo valores de trade_details a Decimal para gating: {e}. TradeDetails: {trade_details_for_side}")
        return trade_details_for_side

    # Obtener precio actual de 1m del WebsocketDataProvider
    df_1m = websocket_data_provider.get_dataframe(symbol, interval_trigger_str)
    if df_1m is None or df_1m.empty:
        logger.warning(f"{log_prefix_1m_gate}: No se pudo obtener DataFrame de {interval_trigger_str} del WDP para gating.")
        return trade_details_for_side
    
    last_1m_candle = df_1m.iloc[-1]
    current_price_1m = None
    if signal_type == 'BUY':
        current_price_1m = Decimal(str(last_1m_candle['low']))
    elif signal_type == 'SELL':
        current_price_1m = Decimal(str(last_1m_candle['high']))
    
    if current_price_1m is None:
        logger.warning(f"{log_prefix_1m_gate}: No se pudo determinar precio actual de 1m (low/high).")
        return trade_details_for_side

    logger.debug(f"{log_prefix_1m_gate}: Precio actual ({'low' if signal_type=='BUY' else 'high'}_{interval_trigger_str}): {current_price_1m}")

    # Lógica de Gating
    place_or_keep_order = False
    if signal_type == 'BUY':
        # Condición de Zona Activa: BBL_orig_5m <= low_1m <= BBM_orig_5m
        if gating_BBL_orig_5m is not None and (gating_BBL_orig_5m <= current_price_1m <= gating_BBM_orig_5m):
            place_or_keep_order = True
            logger.info(f"{log_prefix_1m_gate} COMPRA: Precio {current_price_1m} EN ZONA [{gating_BBL_orig_5m}, {gating_BBM_orig_5m}]. Intentar colocar/mantener orden.")
        else:
            logger.info(f"{log_prefix_1m_gate} COMPRA: Precio {current_price_1m} FUERA DE ZONA [{gating_BBL_orig_5m}, {gating_BBM_orig_5m}]. Cancelar si existe.")
    
    elif signal_type == 'SELL':
        # Condición de Zona Activa: BBM_orig_5m <= high_1m <= BBU_orig_5m
        if gating_BBU_orig_5m is not None and (gating_BBM_orig_5m <= current_price_1m <= gating_BBU_orig_5m):
            place_or_keep_order = True
            logger.info(f"{log_prefix_1m_gate} VENTA: Precio {current_price_1m} EN ZONA [{gating_BBM_orig_5m}, {gating_BBU_orig_5m}]. Intentar colocar/mantener orden.")
        else:
            logger.info(f"{log_prefix_1m_gate} VENTA: Precio {current_price_1m} FUERA DE ZONA [{gating_BBM_orig_5m}, {gating_BBU_orig_5m}]. Cancelar si existe.")

    # Acciones: Colocar, mantener o cancelar orden
    live_order_is_active = False
    order_price_on_exchange = None
    if current_entry_order_id:
        live_order_info = trade_manager.check_order_status(order_id=current_entry_order_id, symbol_target=symbol)
        if live_order_info and live_order_info.get('status') in ['NEW', 'PARTIALLY_FILLED']:
            live_order_is_active = True
            try: order_price_on_exchange = Decimal(str(live_order_info.get('price')))
            except: pass # Mantener None si no se puede convertir
        elif live_order_info and live_order_info.get('status') == 'FILLED':
            logger.info(f"{log_prefix_1m_gate}: Orden {current_entry_order_id} ya LLENADA. _check_entry_order_fill lo manejará.")
            return trade_details_for_side # No hacer nada más aquí, _check_entry_order_fill se encargará
        else: # Cancelada, expirada, rechazada, no encontrada o error
            logger.info(f"{log_prefix_1m_gate}: Orden {current_entry_order_id} ya no está activa (status: {live_order_info.get('status', 'N/A') if live_order_info else 'Error Check'}). Limpiando ID del estado.")
            trade_details_for_side['current_entry_order_id'] = None
            trade_details_for_side['entry_order_response_initial'] = None
            current_entry_order_id = None # Reflejar localmente

    if place_or_keep_order:
        if not live_order_is_active:
            logger.info(f"{log_prefix_1m_gate}: Precio en zona. Colocando NUEVA orden LÍMITE en {target_entry_price_decimal}.")
            binance_order_side = Client.SIDE_BUY if signal_type == "BUY" else Client.SIDE_SELL
            new_placement_result = trade_manager.place_limit_entry_order(
                side=binance_order_side, quantity=quantity, price=target_entry_price_decimal,
                symbol_target=symbol  
            )
            if new_placement_result and new_placement_result.get("orderId") and new_placement_result.get("status") in ["NEW", "PARTIALLY_FILLED"]:
                trade_details_for_side['current_entry_order_id'] = str(new_placement_result.get("orderId"))
                trade_details_for_side['entry_order_response_initial'] = new_placement_result
                logger.info(f"{log_prefix_1m_gate}: Orden {signal_type} colocada: ID {trade_details_for_side['current_entry_order_id']} @ {target_entry_price_decimal}")
            elif new_placement_result and new_placement_result.get("status", "").startswith("ERROR_"):
                logger.error(f"{log_prefix_1m_gate}: Error al colocar orden {signal_type}: {new_placement_result.get('error_message')}")
        elif order_price_on_exchange != target_entry_price_decimal: # Orden activa pero precio objetivo cambió
            logger.info(f"{log_prefix_1m_gate}: Precio en zona. Precio objetivo ({target_entry_price_decimal}) cambió del precio en exchange ({order_price_on_exchange}). Reemplazando orden {current_entry_order_id}.")
            if trade_manager.cancel_order_if_open(order_id=current_entry_order_id, symbol_target=symbol, position_side_for_hedge=position_side_param.get('positionSide')):
                trade_details_for_side['current_entry_order_id'] = None; trade_details_for_side['entry_order_response_initial'] = None; # Limpiar para forzar recolocación
                logger.info(f"{log_prefix_1m_gate}: Orden {current_entry_order_id} cancelada para reemplazo. Se intentará recolocar.")
                # La recolocación ocurrirá en la siguiente iteración si las condiciones persisten.
            else:
                logger.warning(f"{log_prefix_1m_gate}: No se pudo cancelar orden {current_entry_order_id} para reemplazarla.")
        else: # Orden activa y en el precio correcto
             logger.debug(f"{log_prefix_1m_gate}: Precio en zona. Orden {current_entry_order_id} ya activa en {target_entry_price_decimal}. Manteniendo.")
    else: # place_or_keep_order es False (precio fuera de zona)
        if live_order_is_active:
            logger.info(f"{log_prefix_1m_gate}: Precio fuera de zona. Cancelando orden activa {current_entry_order_id}.")
            if trade_manager.cancel_order_if_open(order_id=current_entry_order_id, symbol_target=symbol, position_side_for_hedge=position_side_param.get('positionSide')):
                trade_details_for_side['current_entry_order_id'] = None
                trade_details_for_side['entry_order_response_initial'] = None
        else:
            logger.debug(f"{log_prefix_1m_gate}: Precio fuera de zona. No hay orden activa que cancelar.")
            
    bot_state.set_active_trade(side_being_managed, trade_details_for_side) # Guardar cambios en current_entry_order_id
    return trade_details_for_side
# --- Sección C: Monitoreo de Llenado de Orden de Entrada ---
# (Función _check_entry_order_fill sin cambios, omitida por brevedad)
def _check_entry_order_fill(trade_details_for_side: dict,side_being_managed: str, bot_state: PersistentState,trade_manager: TradeManager, client: Client, position_side_param: dict, symbol: str) -> tuple[dict | None, bool]: #NOSONAR
    # ... (sin cambios)
    current_entry_order_id = trade_details_for_side.get('current_entry_order_id'); log_prefix_fill = f"PENDING_ORDER_MGR ({symbol} - {side_being_managed} - FILL_CHECK)"; logger.debug(f"{log_prefix_fill}: Verificando ID {current_entry_order_id or 'N/A'}")
    if not current_entry_order_id: return trade_details_for_side, False 
    entry_order_info = trade_manager.check_order_status(order_id=current_entry_order_id, symbol_target=symbol, position_side_for_hedge=position_side_param.get('positionSide'))
    if entry_order_info:
        entry_status = entry_order_info.get("status"); logger.debug(f"{log_prefix_fill}: Estado orden {current_entry_order_id}: {entry_status}")
        if entry_status == "FILLED": return _process_filled_order(trade_details_for_side, side_being_managed, entry_order_info, bot_state, trade_manager, f"FILL_CHECK ({symbol})")
        elif entry_status in ["CANCELED", "EXPIRED", "REJECTED", "NOT_FOUND_OR_DELETED"]:
            logger.warning(f"{log_prefix_fill}: Orden {current_entry_order_id} en estado final: {entry_status}. Limpiando ID.")
            if trade_details_for_side.get('current_entry_order_id') == current_entry_order_id: trade_details_for_side['current_entry_order_id'] = None; trade_details_for_side['entry_order_response_initial'] = None; bot_state.set_active_trade(side_being_managed, trade_details_for_side) 
    else: logger.error(f"{log_prefix_fill}: No se pudo obtener info para orden {current_entry_order_id}.")
    return trade_details_for_side, False


# --- Función Principal del Módulo ---
def manage_pending_order(
    trade_details_for_side: dict,
    side_being_managed: str,    
    bot_state: PersistentState,
    trade_manager: TradeManager, 
    client: Client, # Conservado por si algún fallback en display de 1m en BB_buy/sell lo usa. No ideal aquí.
    current_time_local: datetime,
    symbol: str,                
    symbol_params: dict,
    websocket_data_provider: WebsocketDataProvider # <--- AÑADIDO Y USADO
):
    log_prefix_main = f"PENDING_ORDER_MGR ({symbol} - {side_being_managed})"
    logger.info(f"{log_prefix_main}: Gestionando orden pendiente, ID: {trade_details_for_side.get('current_entry_order_id', 'N/A')}")

    # Verificar si websocket_data_provider está disponible
    if not websocket_data_provider:
        logger.error(f"{log_prefix_main}: WebsocketDataProvider no proporcionado a manage_pending_order. No se puede continuar.")
        return # No se puede hacer nada sin WDP para las bandas y datos de 1m

    try: # Chequeo de campos esenciales en trade_details
        if not all(k in trade_details_for_side for k in ['last_5m_bb_update_timestamp_iso', 'signal_type', 
                                                         'pre_check_bbl_orig_primary_str', # Usado para BUY pre-cond
                                                         'pre_check_bbu_orig_primary_str', # Usado para SELL pre-cond
                                                         'pre_check_bbm_15m_str']):       # Usado para ambas pre-cond
            logger.error(f"{log_prefix_main}: Faltan campos esenciales en trade_details para re-verificación. Reseteando trade."); 
            bot_state.clear_active_trade(side_being_managed, f"Faltan campos en trade_details para {symbol}"); return
        last_5m_update_dt_utc = datetime.fromisoformat(trade_details_for_side['last_5m_bb_update_timestamp_iso'])
    except (KeyError, ValueError, TypeError) as e:
        logger.error(f"{log_prefix_main}: Error datos/formato: {e}. Reseteando.", exc_info=True); bot_state.clear_active_trade(side_being_managed, f"Error datos estado dinámico {symbol}: {e}"); return

    current_time_utc = current_time_local.astimezone(pytz.utc)
    position_side_param_dict = {} 
    active_pos_side_config = trade_details_for_side.get("position_side_hedge_mode") 
    if active_pos_side_config in ["LONG", "SHORT"]: position_side_param_dict['positionSide'] = active_pos_side_config

    # --- A. Actualizaciones basadas en el intervalo principal (ej. 5m) ---
    # Esta función actualiza target_entry_price, SL, TP y los valores de bandas en trade_details
    interval_p_minutes = 5; interval_p_str_pom = symbol_params.get('interval_5m', '5m') 
    try:
        if interval_p_str_pom[-1].lower() == 'm' and interval_p_str_pom[:-1].isdigit(): interval_p_minutes = int(interval_p_str_pom[:-1])
        elif interval_p_str_pom[-1].lower() == 'h' and interval_p_str_pom[:-1].isdigit(): interval_p_minutes = int(interval_p_str_pom[:-1]) * 60
    except: pass

    time_since_last_update_seconds = (current_time_utc - last_5m_update_dt_utc).total_seconds()
    is_new_candle_interval = (current_time_local.minute % interval_p_minutes == 0) and \
                             (current_time_local.second < getattr(config, 'LOOP_SLEEP_SECONDS', 15) + 5) # Pequeño margen
    force_update_threshold = (interval_p_minutes * 60) - 30 # Ej. 4.5 min para vela de 5m

    updated_trade_details_after_5m = trade_details_for_side
    should_exit_from_5m_update = False

    if is_new_candle_interval or time_since_last_update_seconds > force_update_threshold:
        logger.info(f"{log_prefix_main}: Realizando _perform_5_minute_updates...")
        updated_trade_details_after_5m, should_exit_from_5m_update = _perform_5_minute_updates(
            trade_details_for_side, side_being_managed, bot_state, trade_manager, 
            current_time_local, current_time_utc, symbol, symbol_params,
            websocket_data_provider=websocket_data_provider
        )
        if should_exit_from_5m_update: logger.info(f"{log_prefix_main}: Saliendo de gestión tras 5m_updates (trade cerrado/llenado)."); return
            
        # Re-obtener estado por si cambió y para asegurar que usamos la versión más fresca
        rechecked_details = bot_state.get_active_trade(side_being_managed)
        if not rechecked_details or rechecked_details.get("status") != "PENDING_DYNAMIC_LIMIT":
            logger.info(f"{log_prefix_main}: Estado ya no es PENDING_DYNAMIC_LIMIT tras 5m_updates. Saliendo."); return
        updated_trade_details_after_5m = rechecked_details # Usar la versión más fresca del estado
    
    # --- B. Re-Verificación de Pre-condición de Señal Original (NUEVO) ---
    # Usar los valores de bandas que _perform_5_minute_updates acaba de actualizar en el estado
    try:
        signal_type_check = updated_trade_details_after_5m['signal_type']
        bbl_orig_5m_check_str = updated_trade_details_after_5m.get('pre_check_bbl_orig_primary_str')
        bbu_orig_5m_check_str = updated_trade_details_after_5m.get('pre_check_bbu_orig_primary_str')
        bbm_15m_check_str = updated_trade_details_after_5m.get('pre_check_bbm_15m_str') # Este es el SL, que es BBM_15m

        if not all([bbl_orig_5m_check_str, bbu_orig_5m_check_str, bbm_15m_check_str]):
            logger.warning(f"{log_prefix_main}: Faltan valores de bandas en trade_details para re-verificación de pre-condición. Saltando chequeo.");
        else:
            bbl_orig_5m_check = Decimal(bbl_orig_5m_check_str)
            bbu_orig_5m_check = Decimal(bbu_orig_5m_check_str)
            bbm_15m_check = Decimal(bbm_15m_check_str)
            pre_condition_still_valid = True

            if signal_type_check == "BUY":
                if not (bbl_orig_5m_check > bbm_15m_check): # Si BBL_orig_5m <= BBM_15m
                    pre_condition_still_valid = False
                    logger.info(f"{log_prefix_main}: Pre-condición de COMPRA (BBL5 > BBM15) ya NO se cumple: {bbl_orig_5m_check} <= {bbm_15m_check}.")
            elif signal_type_check == "SELL":
                if not (bbu_orig_5m_check < bbm_15m_check): # Si BBU_orig_5m >= BBM_15m
                    pre_condition_still_valid = False
                    logger.info(f"{log_prefix_main}: Pre-condición de VENTA (BBU5 < BBM15) ya NO se cumple: {bbu_orig_5m_check} >= {bbm_15m_check}.")
            
            if not pre_condition_still_valid:
                logger.warning(f"{log_prefix_main}: Pre-condición original de la señal ya no es válida. Cancelando trade pendiente para {side_being_managed}.")
                current_order_id_to_cancel = updated_trade_details_after_5m.get('current_entry_order_id')
                if current_order_id_to_cancel:
                    trade_manager.cancel_order_if_open(order_id=current_order_id_to_cancel, symbol_target=symbol, position_side_for_hedge=position_side_param_dict.get('positionSide'))
                bot_state.clear_active_trade(side_being_managed, reason=f"Pre-condición de señal original invalidada para {symbol}")
                return # Salir de la gestión de esta orden pendiente
    except (KeyError, InvalidOperation, TypeError) as e_precheck:
        logger.error(f"{log_prefix_main}: Error convirtiendo valores para re-verificación de pre-condición: {e_precheck}. Saltando chequeo.")

    # --- C. Lógica de Gating de Órdenes de 1 Minuto (Usa la nueva lógica) ---
    # Pasar WDP para que _perform_1_minute_gating obtenga datos de 1m
    interval_trigger_str_pom = symbol_params.get('interval_1m', LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_1M_CFG, '1m'))
    
    updated_trade_details_after_gating = _perform_1_minute_gating(
        updated_trade_details_after_5m, # Usar los detalles actualizados por 5m_updates
        side_being_managed, bot_state, 
        trade_manager, 
        websocket_data_provider, # Pasar WDP
        position_side_param_dict, symbol,
        interval_trigger_str=interval_trigger_str_pom
    )
    
    # Re-obtener del estado por si _perform_1_minute_gating lo modificó (ej. current_entry_order_id)
    current_details_after_gating = bot_state.get_active_trade(side_being_managed)
    if not current_details_after_gating or current_details_after_gating.get("status") != "PENDING_DYNAMIC_LIMIT":
        logger.info(f"{log_prefix_main}: Estado ya no es PENDING_DYNAMIC_LIMIT tras 1min_gating. Saliendo."); return
    final_trade_details = current_details_after_gating

    # --- D. Monitoreo de Llenado de Orden de Entrada ---
    _, should_exit_from_fill_check = _check_entry_order_fill(
        final_trade_details, side_being_managed, bot_state, trade_manager, client, position_side_param_dict, symbol
    )
    if should_exit_from_fill_check: logger.info(f"{log_prefix_main}: Saliendo de gestión tras fill_check (orden llenada o emergencia)."); return
    
    final_details_after_cycle = bot_state.get_active_trade(side_being_managed) # Volver a leer por si _check_entry_order_fill lo cambió
    if final_details_after_cycle: 
        logger.debug(f"{log_prefix_main}: Fin de ciclo de gestión. ID orden: {final_details_after_cycle.get('current_entry_order_id', 'N/A')}")
    else: logger.debug(f"{log_prefix_main}: Fin de ciclo de gestión. El trade fue borrado/completado.")
