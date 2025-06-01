import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation # Añadido InvalidOperation
import pytz # No se usa directamente aquí, pero puede ser útil para logging de timestamps
from datetime import datetime # No se usa directamente aquí

import config # Para USE_MARTINGALE_LOSS_RECOVERY y plantillas de Telegram
from persistent_state import PersistentState
from trade_manager import TradeManager
# bb_utils y Client no se usan directamente en esta función tras los cambios,
# pero se mantienen por si tu TSL los necesita o por importaciones previas.
from bb_utils import get_latest_5m_bollinger_bands_data
from binance.client import Client

logger = logging.getLogger(__name__)

def _send_pos_mgr_notification(telegram_manager, message: str):
    # ... (tu función existente sin cambios)
    logger.info(f"POSITION_MGR: Solicitud para enviar notificación. Telegram Manager: {'Presente' if telegram_manager else 'Ausente'}. Mensaje (primeros 70 chars): '{message[:70]}...'")
    if telegram_manager and hasattr(telegram_manager, 'send_message_to_admin_threadsafe'):
        try:
            logger.debug("POSITION_MGR: Llamando a telegram_manager.send_message_to_admin_threadsafe.")
            telegram_manager.send_message_to_admin_threadsafe(message)
            logger.info("POSITION_MGR: Llamada a send_message_to_admin_threadsafe realizada.")
        except Exception as e:
            logger.error(f"POSITION_MGR: Excepción directa al llamar a send_message_to_admin_threadsafe: {e}", exc_info=True)
    elif telegram_manager:
        logger.warning("POSITION_MGR: telegram_manager presente, pero no tiene el método send_message_to_admin_threadsafe.")
    else:
        logger.warning("POSITION_MGR: telegram_manager es None. No se puede enviar notificación.")

def manage_active_position(
    trade_details_for_side: dict, # Detalles del trade activo desde PersistentState
    side_being_managed: str,      # Clave del estado, ej: "BTCUSDT_LONG"
    bot_state: PersistentState,
    trade_manager: TradeManager,
    symbol: str,                  # Ej: "BTCUSDT"
    symbol_params: dict,          # Config específica del símbolo
    telegram_manager = None,
    websocket_data_provider = None # ✅ AÑADIDO para TSL si usa WDP
):
    log_prefix = f"POSITION_MGR ({symbol} - {side_being_managed})"

    if not trade_details_for_side:
        logger.warning(f"{log_prefix}: Se llamó sin trade_details_for_side. No se puede gestionar.")
        return

    entry_order_response_dict = trade_details_for_side.get("entry_order_response", {})
    entry_order_id_from_state = entry_order_response_dict.get("orderId", "N/A") if isinstance(entry_order_response_dict, dict) else "N/A"
    
    sl_order_details = trade_details_for_side.get("sl_order_response")
    tp_order_details = trade_details_for_side.get("tp_order_response")

    sl_order_id = sl_order_details.get("orderId") if isinstance(sl_order_details, dict) else None
    tp_order_id = tp_order_details.get("orderId") if isinstance(tp_order_details, dict) else None
    
    original_entry_price_str = trade_details_for_side.get("entry_price_actual", "0")
    position_quantity_str = trade_details_for_side.get("quantity", "0")
    original_pos_side_config = trade_details_for_side.get("position_side_hedge_mode", "N/A") # "LONG" o "SHORT"

    try:
        original_entry_price_decimal = Decimal(original_entry_price_str)
        position_quantity_decimal = Decimal(position_quantity_str)
    except InvalidOperation: # Ser más específico con la excepción
        logger.error(f"{log_prefix}: Error convirtiendo precios/cantidades a Decimal. Usando defaults. EP:'{original_entry_price_str}', Qty:'{position_quantity_str}'")
        original_entry_price_decimal = Decimal("0")
        position_quantity_decimal = Decimal("0")

    s_info = trade_manager.get_symbol_info_data(symbol)
    quote_asset = s_info.get('quoteAsset', 'USDT') if s_info else 'USDT'
    price_precision = trade_manager.get_price_precision(symbol)
    qty_precision = s_info.get('quantityPrecision', 8) if s_info else 8

    logger.info(f"{log_prefix}: Gestionando posición. Entrada ID: {entry_order_id_from_state}, SL ID: {sl_order_id or 'N/A'}, TP ID: {tp_order_id or 'N/A'}")

    use_martingale_config = getattr(config, 'USE_MARTINGALE_LOSS_RECOVERY', False)

    # 1. Chequear estado de la orden SL
    if sl_order_id:
        sl_status_info = trade_manager.check_order_status(order_id=sl_order_id, symbol_target=symbol)
        
        if sl_status_info and sl_status_info.get('status') == 'FILLED':
            logger.info(f"{log_prefix}: STOP LOSS ALCANZADO para {symbol} ({original_pos_side_config}). ID Orden SL: {sl_order_id}")
            
            closure_details = trade_manager.fetch_trade_closure_details(
                closing_order_id=int(sl_order_id),
                original_entry_price=original_entry_price_decimal,
                position_quantity=position_quantity_decimal,
                position_side=original_pos_side_config, # "LONG" o "SHORT"
                symbol_target=symbol
            )

            pnl_value = closure_details.get('realized_pnl', Decimal(0)) if closure_details else Decimal(0)
            avg_close_price_value = Decimal(closure_details.get('avg_close_price', sl_status_info.get('avgPrice', "0"))) if closure_details else Decimal(sl_status_info.get('avgPrice', "0"))
            closed_quantity_value = abs(Decimal(closure_details.get('closed_quantity', sl_status_info.get('executedQty', "0")))) if closure_details else abs(Decimal(sl_status_info.get('executedQty', "0")))
            pnl_asset = closure_details.get('commission_asset', quote_asset) if closure_details else quote_asset
            
            current_balance_decimal = trade_manager.fetch_account_balance(asset=quote_asset)
            balance_value = current_balance_decimal if current_balance_decimal is not None else Decimal("0")

            sl_filled_message = config.TELEGRAM_MSG_SL_FILLED.format(
                symbol=symbol, direction=original_pos_side_config,
                quantity=f"{closed_quantity_value:.{qty_precision}f}",
                entry_price=f"{original_entry_price_decimal:.{price_precision}f}",
                close_price=f"{avg_close_price_value:.{price_precision}f}",
                pnl=f"{pnl_value:.2f}", asset_pnl=pnl_asset,
                balance=f"{balance_value:.2f}", asset_balance=quote_asset
            )
            _send_pos_mgr_notification(telegram_manager, sl_filled_message)

            # ✅ --- LÓGICA MARTINGALA --- ✅
            if use_martingale_config:
                if pnl_value < Decimal(0): # Es una pérdida
                    loss_to_add = abs(pnl_value)
                    logger.info(f"{log_prefix}: SL HIT - Registrando pérdida para Martingala: {loss_to_add} {pnl_asset} para {side_being_managed}")
                    bot_state.update_accumulated_loss(side_being_managed, loss_to_add)
                # No es necesario resetear accumulated_loss en un SL hit, solo se acumula.
            
            if tp_order_id: # Cancelar TP si aún existe
                logger.info(f"{log_prefix}: Intentando cancelar orden TP ({tp_order_id}) restante tras SL hit.")
                trade_manager.cancel_order_if_open(order_id=tp_order_id, symbol_target=symbol)
            
            bot_state.clear_active_trade(side_being_managed, reason=f"SL Hit @ {avg_close_price_value:.{price_precision}f} para {symbol}")
            alert_key_no_sl = f"{side_being_managed}_NO_SL_ALERT_SENT"
            if bot_state.get_active_trade(alert_key_no_sl):
                bot_state.clear_active_trade(alert_key_no_sl, "Position closed by TP")
            return 

        elif sl_status_info and sl_status_info.get('status') not in ['NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL']:
            logger.warning(f"{log_prefix}: Orden SL ID {sl_order_id} ya no está activa (Estado: {sl_status_info.get('status')}). Se tratará como 'Posición sin SL'.")
            sl_order_id = None # Para que la lógica de "sin SL" se active si es necesario
            trade_details_for_side['sl_order_response'] = None
            bot_state.set_active_trade(side_being_managed, trade_details_for_side) # Guardar el cambio


    # 2. Chequear estado de la orden TP (solo si SL no se llenó)
    if tp_order_id:
        tp_status_info = trade_manager.check_order_status(order_id=tp_order_id, symbol_target=symbol)

        if tp_status_info and tp_status_info.get('status') == 'FILLED':
            logger.info(f"{log_prefix}: TAKE PROFIT ALCANZADO para {symbol} ({original_pos_side_config}). ID Orden TP: {tp_order_id}")

            closure_details = trade_manager.fetch_trade_closure_details(
                closing_order_id=int(tp_order_id),
                original_entry_price=original_entry_price_decimal,
                position_quantity=position_quantity_decimal,
                position_side=original_pos_side_config,
                symbol_target=symbol
            )
            pnl_value = closure_details.get('realized_pnl', Decimal(0)) if closure_details else Decimal(0)
            avg_close_price_value = Decimal(closure_details.get('avg_close_price', tp_status_info.get('avgPrice', "0"))) if closure_details else Decimal(tp_status_info.get('avgPrice', "0"))
            closed_quantity_value = abs(Decimal(closure_details.get('closed_quantity', tp_status_info.get('executedQty', "0")))) if closure_details else abs(Decimal(tp_status_info.get('executedQty', "0")))
            pnl_asset = closure_details.get('commission_asset', quote_asset) if closure_details else quote_asset

            current_balance_decimal = trade_manager.fetch_account_balance(asset=quote_asset)
            balance_value = current_balance_decimal if current_balance_decimal is not None else Decimal("0")

            tp_filled_message = config.TELEGRAM_MSG_TP_FILLED.format(
                symbol=symbol, direction=original_pos_side_config,
                quantity=f"{closed_quantity_value:.{qty_precision}f}",
                entry_price=f"{original_entry_price_decimal:.{price_precision}f}",
                close_price=f"{avg_close_price_value:.{price_precision}f}",
                pnl=f"{pnl_value:.2f}", asset_pnl=pnl_asset,
                balance=f"{balance_value:.2f}", asset_balance=quote_asset
            )
            _send_pos_mgr_notification(telegram_manager, tp_filled_message)

            # ✅ --- LÓGICA MARTINGALA --- ✅
            if use_martingale_config:
                if pnl_value >= Decimal(0): # Es una ganancia (o breakeven)
                    accumulated_loss_at_entry_str = trade_details_for_side.get('accumulated_loss_at_entry', '0')
                    try:
                        accumulated_loss_when_trade_opened = Decimal(accumulated_loss_at_entry_str)
                    except InvalidOperation:
                        accumulated_loss_when_trade_opened = Decimal('0')
                    
                    if accumulated_loss_when_trade_opened > Decimal(0):
                        logger.info(f"{log_prefix}: TP HIT - Trade ganador fue parte de secuencia Martingala (pérdida acumulada al abrir: {accumulated_loss_when_trade_opened}). "
                                    f"Reseteando pérdidas acumuladas para {side_being_managed}.")
                        bot_state.reset_accumulated_loss(side_being_managed)
                    else:
                        logger.info(f"{log_prefix}: TP HIT - Trade ganador, no había pérdidas acumuladas para Martingala en {side_being_managed}.")
                # Si es una pérdida pero se cerró por TP (raro, pero posible si TP < Entrada en SHORT o TP > Entrada en LONG por error),
                # la lógica de SL HIT ya debería haberla cubierto si el PnL es negativo.
                # Aquí solo nos interesa resetear si fue un TP hit Y recuperó pérdidas.
            
            if sl_order_id: # sl_order_id pudo haberse puesto a None arriba
                logger.info(f"{log_prefix}: Intentando cancelar orden SL ({sl_order_id}) restante tras TP hit.")
                trade_manager.cancel_order_if_open(order_id=sl_order_id, symbol_target=symbol)

            bot_state.clear_active_trade(side_being_managed, reason=f"TP Hit @ {avg_close_price_value:.{price_precision}f} para {symbol}")
            alert_key_no_sl = f"{side_being_managed}_NO_SL_ALERT_SENT"
            if bot_state.get_active_trade(alert_key_no_sl):
                bot_state.clear_active_trade(alert_key_no_sl, "Position closed by TP")
            return 

        elif tp_status_info and tp_status_info.get('status') not in ['NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL']:
            logger.warning(f"{log_prefix}: Orden TP ID {tp_order_id} ya no está activa (Estado: {tp_status_info.get('status')}).")


    # 3. Verificar si la posición sigue existiendo en Binance (si SL/TP no se llenaron)
    current_position_on_exchange = trade_manager.get_active_position(
        side_to_check=original_pos_side_config, 
        symbol_target=symbol
    )

    if not current_position_on_exchange or current_position_on_exchange.get("amount", Decimal(0)) == Decimal(0):
        if bot_state.get_active_trade(side_being_managed): # Solo si el bot pensaba que tenía un trade
            logger.warning(f"{log_prefix}: Posición para {symbol} ya no existe en exchange, pero SL/TP no reportaron FILL. "
                           f"SL ID: {sl_order_id or 'N/A'}, TP ID: {tp_order_id or 'N/A'}. Limpiando estado.")
            _send_pos_mgr_notification(telegram_manager, 
                                   f"⚠️ ALERTA ({symbol}): Posición {original_pos_side_config} "
                                   f"desapareció del exchange sin SL/TP FILL. Revisar cuenta y PnL manualmente. "
                                   f"Pérdida acumulada para Martingala NO se actualizará automáticamente para este cierre.")
            
            # Cancelar órdenes remanentes por si acaso
            current_sl_id_in_state = trade_details_for_side.get("sl_order_response", {}).get("orderId")
            current_tp_id_in_state = trade_details_for_side.get("tp_order_response", {}).get("orderId")
            if current_sl_id_in_state: trade_manager.cancel_order_if_open(order_id=current_sl_id_in_state, symbol_target=symbol)
            if current_tp_id_in_state: trade_manager.cancel_order_if_open(order_id=current_tp_id_in_state, symbol_target=symbol)
            
            bot_state.clear_active_trade(side_being_managed, reason=f"Posición {symbol} no encontrada en exchange (cierre desconocido)")
            alert_key_no_sl = f"{side_being_managed}_NO_SL_ALERT_SENT"
            if bot_state.get_active_trade(alert_key_no_sl):
                bot_state.clear_active_trade(alert_key_no_sl, "Position disappeared from exchange")
        return # Salir porque no hay posición que gestionar

    # --- LÓGICA TRAILING STOP LOSS (TSL) ---
    # Tu lógica de TSL existente iría aquí. Asegúrate que usa websocket_data_provider si es necesario.
    # Si el TSL actualiza el SL, y ese nuevo SL es alcanzado, la lógica de "SL HIT" de arriba lo manejará.
    # Si el TSL falla y deja la posición sin SL, la lógica de "Posición sin SL activo" de abajo lo manejará.
    # Ejemplo simplificado de dónde iría:
    if getattr(config, 'ENABLE_TRAILING_STOP_LOSS_ORIG_BANDS', False) and \
       trade_details_for_side.get("sl_order_response") and \
       isinstance(trade_details_for_side.get("sl_order_response"), dict) and \
       trade_details_for_side.get("sl_order_response", {}).get("orderId"):
        
        logger.debug(f"{log_prefix}: Iniciando lógica de Trailing Stop Loss (BB_orig).")
        # ... (Aquí va tu código completo de TSL, asegurándote de que:
        #      - Obtiene los datos de BB actualizados, preferiblemente usando websocket_data_provider
        #        pasado a esta función. Si get_latest_5m_bollinger_bands_data ya usa WDP, está bien.
        #      - Si actualiza el SL, guarda la nueva 'sl_order_response' en bot_state:
        #        trade_details_for_side['sl_order_response'] = new_sl_response
        #        bot_state.set_active_trade(side_being_managed, trade_details_for_side)
        #      - La variable 'sl_order_id' local se actualiza si el TSL cambia el ID de la orden SL.
        # )
        # Ejemplo de llamada (necesitarás el 'client' si get_latest_5m_bollinger_bands_data lo requiere y no usa WDP)
        # Y los parámetros de BB de symbol_params
        # binance_api_client_for_tsl = trade_manager.client # o directamente binance_client si lo pasas a manage_active_position
        # latest_bb_data = get_latest_5m_bollinger_bands_data(
        #     # client=binance_api_client_for_tsl, # Solo si es necesario
        #     websocket_data_provider=websocket_data_provider, # Pasar el WDP
        #     symbol=symbol, 
        #     interval_5m_str=symbol_params.get('interval_5m', '5m'), # o el que uses para TSL
        #     ma_type_pine=symbol_params.get('ma_type', 'SMA'), 
        #     length=int(symbol_params.get('length', 20)), 
        #     mult_orig=float(symbol_params.get('mult_orig', 2.0)), 
        #     mult_new=float(symbol_params.get('mult_new', 1.0))
        # )
        # if latest_bb_data and not latest_bb_data.get("error"):
            # ... tu lógica TSL que podría modificar sl_order_id y sl_order_details ...
            # Si el SL se modifica, actualiza sl_order_id para la lógica de "sin SL activo" más abajo.
            # new_sl_response = trade_manager.place_stop_loss_order(...)
            # if new_sl_response and new_sl_response.get('orderId'):
            #    trade_details_for_side['sl_order_response'] = new_sl_response
            #    bot_state.set_active_trade(side_being_managed, trade_details_for_side)
            #    sl_order_id = new_sl_response.get('orderId') # Actualizar ID local para el chequeo de "sin SL"
    
    # 4. Lógica para "Posición Abierta sin SL Activo"
    # Re-evaluar sl_order_id porque pudo haber cambiado por TSL o si el original fue cancelado.
    final_sl_order_details_from_state = bot_state.get_active_trade(side_being_managed).get("sl_order_response", {}) # type: ignore
    final_sl_order_id_from_state = final_sl_order_details_from_state.get("orderId") if isinstance(final_sl_order_details_from_state, dict) else None
    
    sl_order_is_currently_active_final_check = False
    if final_sl_order_id_from_state:
        final_current_sl_status_info = trade_manager.check_order_status(order_id=final_sl_order_id_from_state, symbol_target=symbol)
        if final_current_sl_status_info and final_current_sl_status_info.get('status') in ['NEW', 'PARTIALLY_FILLED']:
            sl_order_is_currently_active_final_check = True
    
    alert_key_no_sl = f"{side_being_managed}_NO_SL_ALERT_SENT" # Clave única para el estado de la alerta

    if not sl_order_is_currently_active_final_check:
        if not bot_state.get_active_trade(alert_key_no_sl): # Enviar alerta solo una vez
            # ... (tu código para enviar TELEGRAM_MSG_POSITION_NO_SL_ALERT) ...
            # Ejemplo:
            pos_amount_alert = current_position_on_exchange.get('amount', position_quantity_decimal) if current_position_on_exchange else position_quantity_decimal
            pos_entry_alert = current_position_on_exchange.get('entry_price', original_entry_price_decimal) if current_position_on_exchange else original_entry_price_decimal
            
            no_sl_alert_message = config.TELEGRAM_MSG_POSITION_NO_SL_ALERT.format(
                symbol=symbol, direction=original_pos_side_config,
                quantity=f"{abs(pos_amount_alert):.{qty_precision}f}", # abs por si es negativo
                entry_price=f"{pos_entry_alert:.{price_precision}f}"
            )
            _send_pos_mgr_notification(telegram_manager, no_sl_alert_message)
            bot_state.set_active_trade(alert_key_no_sl, {"alert_sent_timestamp": datetime.now(pytz.utc).isoformat()})
            logger.warning(f"{log_prefix}: Posición {symbol} ({original_pos_side_config}) abierta SIN SL activo. Alerta enviada.")
    else: 
        if bot_state.get_active_trade(alert_key_no_sl): # Si la alerta existía y ahora hay SL
            bot_state.clear_active_trade(alert_key_no_sl, "SL for position is now active.")
            logger.info(f"{log_prefix}: SL para {symbol} ({original_pos_side_config}) ahora está activo. Alerta 'No SL' (si existía) borrada.")

    logger.debug(f"{log_prefix}: Fin de ciclo de gestión de posición activa para {symbol}.")