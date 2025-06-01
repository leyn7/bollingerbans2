# Archivo: trade_manager.py
import logging
from decimal import Decimal, ROUND_DOWN, getcontext
from binance.client import Client # Para enums y constantes
from binance.exceptions import BinanceAPIException, BinanceOrderException

# --- AÑADIR ESTAS IMPORTACIONES ---
from binance_utils import get_trade_closure_details, get_futures_account_balance
# --- FIN DE IMPORTACIONES A AÑADIR ---

logger = logging.getLogger(__name__) 
LOG_PREFIX_TRADE_MANAGER = "TradeManager" 
getcontext().prec = 18 

class TradeManager:
    def __init__(self, client: Client, symbol: str):
        self.client = client
        self.symbol = symbol.upper()
        self.symbol_info_cache = {} 
        self.symbol_info = None # Para mantener compatibilidad si alguna parte aún lo usa directamente
        self._fetch_and_cache_symbol_info_for_init(self.symbol) 

    def _fetch_and_cache_symbol_info_for_init(self, symbol_to_fetch: str):
        # ... (código existente sin cambios)
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (_fetch_and_cache_symbol_info_for_init - {symbol_to_fetch})"
        logger.info(f"{log_ctx}: Obteniendo información de exchange para {symbol_to_fetch}...")
        try:
            exchange_info = self.client.futures_exchange_info()
            for s_data in exchange_info['symbols']:
                if s_data['symbol'] == symbol_to_fetch:
                    lot_size_filter = next((f for f in s_data.get('filters', []) if f.get('filterType') == 'LOT_SIZE'), None)
                    min_notional_filter = next((f for f in s_data.get('filters', []) if f.get('filterType') == 'MIN_NOTIONAL'), None)
                    price_filter = next((f for f in s_data.get('filters', []) if f.get('filterType') == 'PRICE_FILTER'), None)

                    if not lot_size_filter or not min_notional_filter or not price_filter:
                        msg = f"Filtros esenciales no encontrados para {symbol_to_fetch}."
                        logger.error(f"{log_ctx}: {msg}")
                        raise ValueError(msg)

                    detailed_info = {
                        'pricePrecision': int(s_data.get('pricePrecision', 8)),
                        'quantityPrecision': int(s_data.get('quantityPrecision', 8)),
                        'baseAsset': s_data.get('baseAsset'),
                        'quoteAsset': s_data.get('quoteAsset'),
                        'minQty': Decimal(lot_size_filter.get('minQty', '0')),
                        'stepSize': Decimal(lot_size_filter.get('stepSize', '0')),
                        'minNotional': Decimal(min_notional_filter.get('minNotional', '0')),
                        'tickSize': Decimal(price_filter.get('tickSize', '0')),
                        'status': s_data.get('status', 'UNKNOWN'),
                        'raw_filters': s_data.get('filters', []) 
                    }
                    self.symbol_info_cache[symbol_to_fetch] = detailed_info
                    if symbol_to_fetch == self.symbol: # Asignar también a self.symbol_info
                        self.symbol_info = detailed_info

                    logger.info(f"{log_ctx}: Info del símbolo {symbol_to_fetch} cacheada. Tick: {detailed_info['tickSize']}, Step: {detailed_info['stepSize']}")
                    return detailed_info
            
            msg = f"Símbolo {symbol_to_fetch} no encontrado."
            logger.error(f"{log_ctx}: {msg}")
            self.symbol_info_cache[symbol_to_fetch] = None 
            if symbol_to_fetch == self.symbol:
                self.symbol_info = None
            raise ValueError(msg)
        except BinanceAPIException as e:
            logger.error(f"{log_ctx}: Excepción API obteniendo info para {symbol_to_fetch}: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"{log_ctx}: Excepción obteniendo info para {symbol_to_fetch}: {e}", exc_info=True)
            raise

    def get_symbol_info_data(self, symbol_target: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        if target not in self.symbol_info_cache or self.symbol_info_cache.get(target) is None:
            try:
                self._fetch_and_cache_symbol_info_for_init(target)
            except ValueError: 
                return None 
        
        s_info = self.symbol_info_cache.get(target)
        if not s_info: 
            logger.error(f"No se pudo cargar symbol_info para {target} en get_symbol_info_data.")
            return None
        return s_info

    # ... (resto de funciones existentes como adjust_price_to_tick_size, adjust_quantity_to_step_size, etc. sin cambios)
    def adjust_price_to_tick_size(self, price: Decimal, symbol_target: str = None) -> Decimal | None:
        target = symbol_target.upper() if symbol_target else self.symbol
        s_info = self.get_symbol_info_data(target)
        if not s_info: return price 

        tick_size = s_info['tickSize']
        if tick_size is None or tick_size <= Decimal(0):
            logger.error(f"{LOG_PREFIX_TRADE_MANAGER} ({target}): tick_size inválido ({tick_size}).")
            return price 
        
        num_decimals = abs(tick_size.as_tuple().exponent)
        adjusted_price = (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size
        return adjusted_price.quantize(Decimal('1e-' + str(num_decimals)))

    def adjust_quantity_to_step_size(self, quantity: Decimal, symbol_target: str = None) -> Decimal | None:
        target = symbol_target.upper() if symbol_target else self.symbol
        s_info = self.get_symbol_info_data(target)
        if not s_info: return None 

        step_size = s_info['stepSize']
        min_qty = s_info['minQty']

        if step_size is None or step_size < Decimal(0): 
            logger.error(f"{LOG_PREFIX_TRADE_MANAGER} ({target}): step_size inválido ({step_size}).")
            return None
        if quantity < min_qty:
            logger.warning(f"{LOG_PREFIX_TRADE_MANAGER} ({target}): Cantidad {quantity} < minQty {min_qty}.")
            return None

        if step_size == Decimal(0): 
            adjusted_quantity = quantity.to_integral_value(rounding=ROUND_DOWN)
        else:
            adjusted_quantity = (quantity / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size
        
        if adjusted_quantity < min_qty:
            logger.warning(f"{LOG_PREFIX_TRADE_MANAGER} ({target}): Cantidad ajustada {adjusted_quantity} < minQty {min_qty}.")
            return None
        return adjusted_quantity

    def _format_order_price(self, price: Decimal, symbol_target: str = None) -> str:
        target = symbol_target.upper() if symbol_target else self.symbol
        s_info = self.get_symbol_info_data(target)
        if not s_info: return str(price) 
        return f"{price:.{s_info['pricePrecision']}f}"

    def _format_order_quantity(self, quantity: Decimal, symbol_target: str = None) -> str:
        target = symbol_target.upper() if symbol_target else self.symbol
        s_info = self.get_symbol_info_data(target)
        if not s_info: return str(quantity) 
        return f"{quantity:.{s_info['quantityPrecision']}f}"

    def set_leverage(self, leverage: int, symbol: str = None) -> bool:
        # ... (código existente sin cambios)
        target_symbol = symbol.upper() if symbol else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (set_leverage - {target_symbol})"
        try:
            self.client.futures_change_leverage(symbol=target_symbol, leverage=leverage)
            logger.info(f"{log_ctx}: Apalancamiento para {target_symbol} configurado a {leverage}x.")
            return True
        except BinanceAPIException as e:
            logger.error(f"{log_ctx}: Error API configurando apalancamiento para {target_symbol} a {leverage}x: {e}")
            if e.code == -4043: 
                logger.info(f"{log_ctx}: Apalancamiento para {target_symbol} ya estaba en {leverage}x o no se pudo cambiar (Error {e.code}).")
                return True 
            return False
        except Exception as e:
            logger.error(f"{log_ctx}: Excepción inesperada configurando apalancamiento: {e}", exc_info=True)
            return False
            
    def is_hedge_mode_active(self) -> bool:
        # ... (código existente sin cambios)
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (is_hedge_mode_active)"
        try:
            position_mode_status = self.client.futures_get_position_mode() 
            is_hedge = position_mode_status.get('dualSidePosition', False) 
            logger.info(f"{log_ctx}: Estado del modo dual (Hedge Mode) recuperado: {is_hedge}")
            return is_hedge
        except BinanceAPIException as e:
            logger.warning(f"{log_ctx}: Error API al verificar el modo de posición: {e}. Asumiendo One-Way (no cobertura).")
            return False 
        except AttributeError as ae: 
            logger.error(f"{log_ctx}: AttributeError al llamar a futures_get_position_mode(): {ae}. Verifica la versión de python-binance. Asumiendo One-Way.")
            return False
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado verificando el modo cobertura: {e}", exc_info=True)
            return False 

    def place_limit_entry_order(self, side: str, quantity: Decimal, price: Decimal, 
                                  time_in_force: str = "GTC", client_order_id: str = None,
                                  symbol_target: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (PlaceLimitEntry - {target})"
        s_info = self.get_symbol_info_data(target)
        if not s_info: 
            msg = "No se pudo obtener información del símbolo."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_SYMBOL_INFO", "error_message": msg}

        adj_quantity = self.adjust_quantity_to_step_size(quantity, target)
        adj_price = self.adjust_price_to_tick_size(price, target)

        if not adj_quantity or not adj_price:
            msg = f"Cantidad ({adj_quantity}) o precio ({adj_price}) inválido tras ajuste."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_ADJUSTMENT", "error_message": msg}
        
        notional_value = adj_quantity * adj_price
        if notional_value < s_info['minNotional']:
            msg = f"Valor nocional {notional_value} < MinNotional {s_info['minNotional']}."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_MIN_NOTIONAL", "error_message": msg}

        params = {
            "symbol": target, "side": side, "type": Client.ORDER_TYPE_LIMIT,
            "quantity": self._format_order_quantity(adj_quantity, target),
            "price": self._format_order_price(adj_price, target),
            "timeInForce": time_in_force,
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
            
        if self.is_hedge_mode_active():
            params['positionSide'] = "LONG" if side == Client.SIDE_BUY else "SHORT"
            logger.info(f"{log_ctx}: Modo Cobertura. positionSide: {params['positionSide']}")
        
        try:
            logger.info(f"{log_ctx}: Intentando orden LÍMITE: {params}")
            order_response = self.client.futures_create_order(**params)
            logger.info(f"{log_ctx}: Orden LÍMITE de entrada colocada. ID: {order_response.get('orderId')}, Estado: {order_response.get('status')}")
            return order_response
        except (BinanceAPIException, BinanceOrderException) as e:
            logger.error(f"{log_ctx}: Error al colocar orden LÍMITE de entrada: {e}", exc_info=False) 
            return {"status": "ERROR_API_ORDER", "error_message": str(e), "exception_code": e.code if hasattr(e, 'code') else None}
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado al colocar orden LÍMITE de entrada: {e}", exc_info=True)
            return {"status": "ERROR_UNEXPECTED", "error_message": str(e)}


    def place_stop_loss_order(self, position_asset_side: str, sl_price: Decimal, 
                                symbol_target: str = None, client_order_id: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (PlaceSL - {target})"
        
        order_side = Client.SIDE_SELL if position_asset_side == Client.SIDE_BUY else Client.SIDE_BUY
        adj_sl_price = self.adjust_price_to_tick_size(sl_price, target)

        if not adj_sl_price:
            msg = f"Precio SL ({adj_sl_price}) inválido tras ajuste."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_ADJUSTMENT", "error_message": msg}

        params = {
            "symbol": target, 
            "side": order_side, 
            "type": Client.FUTURE_ORDER_TYPE_STOP_MARKET, 
            "stopPrice": self._format_order_price(adj_sl_price, target),
            "closePosition": "true" 
        }

        if client_order_id:
            params["newClientOrderId"] = client_order_id

        if self.is_hedge_mode_active():
            params['positionSide'] = "LONG" if position_asset_side == Client.SIDE_BUY else "SHORT"
            logger.info(f"{log_ctx}: Modo Cobertura. positionSide: {params['positionSide']}")
            
        try:
            logger.info(f"{log_ctx}: Intentando orden SL (STOP_MARKET): {params}")
            order_response = self.client.futures_create_order(**params)
            logger.info(f"{log_ctx}: Orden SL colocada. ID: {order_response.get('orderId')}, Estado: {order_response.get('status')}")
            return order_response
        except (BinanceAPIException, BinanceOrderException) as e:
            logger.error(f"{log_ctx}: Error al colocar orden SL: {e}", exc_info=False)
            return {"status": "ERROR_API_ORDER", "error_message": str(e), "exception_code": e.code if hasattr(e, 'code') else None}
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado al colocar orden SL: {e}", exc_info=True)
            return None

    def place_take_profit_order(self, position_asset_side: str, tp_price: Decimal, 
                                  symbol_target: str = None, client_order_id: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (PlaceTP - {target})"

        order_side = Client.SIDE_SELL if position_asset_side == Client.SIDE_BUY else Client.SIDE_BUY
        adj_tp_price = self.adjust_price_to_tick_size(tp_price, target)

        if not adj_tp_price:
            msg = f"Precio TP ({adj_tp_price}) inválido tras ajuste."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_ADJUSTMENT", "error_message": msg}

        params = {
            "symbol": target, 
            "side": order_side, 
            "type": Client.FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET, 
            "stopPrice": self._format_order_price(adj_tp_price, target),
            "closePosition": "true" 
        }

        if client_order_id:
            params["newClientOrderId"] = client_order_id
            
        if self.is_hedge_mode_active():
            params['positionSide'] = "LONG" if position_asset_side == Client.SIDE_BUY else "SHORT"
            logger.info(f"{log_ctx}: Modo Cobertura. positionSide: {params['positionSide']}")

        try:
            logger.info(f"{log_ctx}: Intentando orden TP (TAKE_PROFIT_MARKET): {params}")
            order_response = self.client.futures_create_order(**params)
            logger.info(f"{log_ctx}: Orden TP colocada. ID: {order_response.get('orderId')}, Estado: {order_response.get('status')}")
            return order_response
        except (BinanceAPIException, BinanceOrderException) as e:
            logger.error(f"{log_ctx}: Error al colocar orden TP: {e}", exc_info=False)
            return {"status": "ERROR_API_ORDER", "error_message": str(e), "exception_code": e.code if hasattr(e, 'code') else None}
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado al colocar orden TP: {e}", exc_info=True)
            return None

    def place_limit_entry_with_market_sl_tp(
        self, 
        side: str, 
        quantity: Decimal, 
        entry_price: Decimal, 
        sl_price: Decimal, 
        tp_price: Decimal,
        time_in_force: str = "GTC"
    ):
        # ... (código existente sin cambios)
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (LegacyPlaceBracket - {self.symbol})"
        logger.warning(f"{log_ctx}: Usando función 'place_limit_entry_with_market_sl_tp'. Considera las funciones granulares para la nueva lógica.")

        entry_order_response = self.place_limit_entry_order(
            side=side, 
            quantity=quantity, 
            price=entry_price, 
            time_in_force=time_in_force,
            symbol_target=self.symbol 
        )

        if not entry_order_response or entry_order_response.get("status", "").startswith("ERROR_"):
            logger.error(f"{log_ctx}: Falló la colocación de la orden de entrada Límite. No se colocarán SL/TP.")
            return {
                "status": "ERROR_ENTRY_FAILED",
                "entry_order_response": entry_order_response,
                "sl_order_response": None,
                "tp_order_response": None,
                "error_message": entry_order_response.get("error_message", "Fallo en la entrada")
            }

        logger.info(f"{log_ctx}: Orden de entrada Límite colocada (ID: {entry_order_response.get('orderId')}). SL/TP deben ser colocados por el llamador DESPUÉS del llenado.")
        
        return {
            "status": "ENTRY_ORDER_PLACED",
            "entry_order_response": entry_order_response,
            "sl_order_response": None,
            "tp_order_response": None,
            "side": side, 
            "quantity": quantity, 
            "sl_price_target": sl_price, 
            "tp_price_target": tp_price 
        }

    def get_active_position(self, side_to_check: str = None, symbol_target: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (GetActivePosition - {target})"
        logger.debug(f"{log_ctx}: Verificando posición activa para {target}"
                      f"{(' lado específico: ' + side_to_check) if side_to_check else ''}.")
        try:
            positions_info = self.client.futures_position_information(symbol=target)
            
            if not positions_info:
                logger.debug(f"{log_ctx}: No se recibió información de posición para {target}.")
                return None

            for p_info in positions_info:
                if p_info['symbol'] == target:
                    pos_amt_str = p_info.get('positionAmt', '0')
                    try:
                        pos_amt = Decimal(pos_amt_str)
                    except Exception:
                        logger.warning(f"{log_ctx}: No se pudo convertir positionAmt '{pos_amt_str}' a Decimal para {target}. Omitiendo.")
                        continue

                    if pos_amt != Decimal(0):
                        api_position_side = p_info.get('positionSide', 'BOTH')
                        
                        position_details = {
                            "symbol": p_info['symbol'],
                            "amount": pos_amt,
                            "entry_price": Decimal(p_info.get('entryPrice', '0')),
                            "side_inferred": "BUY" if pos_amt > 0 else "SELL", 
                            "unrealized_pnl": Decimal(p_info.get('unRealizedProfit', '0')),
                            "positionSide_api": api_position_side
                        }

                        is_hedge = self.is_hedge_mode_active() 
                        if is_hedge:
                            if side_to_check:
                                if api_position_side == side_to_check:
                                    logger.info(f"{log_ctx}: Posición activa encontrada para {target}, Lado Específico: {side_to_check}, Cant: {pos_amt}")
                                    return position_details
                            else:
                                logger.warning(f"{log_ctx}: Modo cobertura, pero no se especificó 'side_to_check'. "
                                               f"Devolviendo primera activa para {target} (Lado API: {api_position_side}, Cant: {pos_amt}).")
                                return position_details
                        else: 
                            logger.info(f"{log_ctx}: Posición activa encontrada para {target} (Modo Unidireccional), Cant: {pos_amt}")
                            return position_details
            
            logger.debug(f"{log_ctx}: No se encontró posición activa para {target} que coincida."
                         f"{(' (lado: ' + side_to_check + ')') if side_to_check and self.is_hedge_mode_active() else ''}.")
            return None
        except BinanceAPIException as e:
            logger.error(f"{log_ctx}: Error API obteniendo info de posición: {e}")
            return None
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado obteniendo info de posición: {e}", exc_info=True)
            return None

    def check_order_status(self, order_id: str = None, client_order_id: str = None, 
                           symbol_target: str = None, position_side_for_hedge: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (CheckOrderStatus - {target})"
        params = {"symbol": target}
        identifier_for_log = ""

        if order_id:
            params["orderId"] = str(order_id)
            identifier_for_log = f"orderId {order_id}"
        elif client_order_id:
            params["origClientOrderId"] = str(client_order_id)
            identifier_for_log = f"clientOrderId {client_order_id}"
        else:
            logger.error(f"{log_ctx}: Se requiere order_id o client_order_id.")
            return None
            
        try:
            order = self.client.futures_get_order(**params)
            return order
        except BinanceAPIException as e:
            logger.error(f"{log_ctx}: Error API verificando orden {identifier_for_log}: {e}")
            if e.code == -2013: 
                return {"status": "NOT_FOUND_OR_DELETED", "message": str(e), "orderId": str(order_id) if order_id else None} 
            return {"status": "ERROR_API_CHECK", "message": str(e), "orderId": str(order_id) if order_id else None, "exception_code": e.code}
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado verificando orden {identifier_for_log}: {e}", exc_info=True)
            return {"status": "ERROR_UNEXPECTED_CHECK", "message": str(e), "orderId": str(order_id) if order_id else None}

    def cancel_order_if_open(self, order_id: str = None, client_order_id: str = None, 
                             symbol_target: str = None, position_side_for_hedge: str = None) -> bool:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (CancelOrderIfOpen - {target})"
        
        if not order_id and not client_order_id:
            logger.error(f"{log_ctx}: Se necesita order_id o client_order_id para cancelar.")
            return False
        
        order_info = self.check_order_status(order_id=order_id, client_order_id=client_order_id, 
                                             symbol_target=target, position_side_for_hedge=position_side_for_hedge)
        
        if order_info and order_info.get('status'):
            status = order_info['status']
            id_for_cancel_op = order_info.get('orderId', order_id) 
            log_id_display = id_for_cancel_op if id_for_cancel_op else client_order_id

            if status in ['NEW', 'PARTIALLY_FILLED', 'PENDING_CANCEL']: 
                try:
                    cancel_params = {"symbol": target}
                    if id_for_cancel_op:
                        cancel_params['orderId'] = str(id_for_cancel_op)
                    elif client_order_id and not id_for_cancel_op :
                         cancel_params['origClientOrderId'] = str(client_order_id)
                    else:
                        logger.error(f"{log_ctx}: No hay identificador válido para cancelar orden {log_id_display} (Estado: {status}).")
                        return False
                    
                    self.client.futures_cancel_order(**cancel_params)
                    logger.info(f"{log_ctx}: Orden {log_id_display} (Estado: {status}) cancelada exitosamente.")
                    return True
                except BinanceAPIException as e:
                    logger.error(f"{log_ctx}: Error API cancelando orden {log_id_display} (Estado: {status}): {e}")
                    if e.code == -2011: 
                        logger.info(f"{log_ctx}: Orden {log_id_display} ya cerrada/cancelada (Error -2011).")
                        return True 
                    return False
                except Exception as e:
                    logger.error(f"{log_ctx}: Error inesperado cancelando orden {log_id_display} (Estado: {status}): {e}", exc_info=True)
                    return False
            elif status in ['FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'NOT_FOUND_OR_DELETED']:
                logger.info(f"{log_ctx}: Orden {log_id_display} ya en estado final ({status}). No se cancela.")
                return True 
            else: 
                logger.warning(f"{log_ctx}: Estado desconocido ({status}) para orden {log_id_display}. Cancelación omitida.")
                return False
        else:
            logger.warning(f"{log_ctx}: No se pudo obtener estado para orden {order_id or client_order_id}. Cancelación omitida.")
            return False

    def get_current_market_price(self, symbol_target: str = None) -> Decimal | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (GetCurrentMarketPrice - {target})"
        try:
            mark_price_info = self.client.futures_mark_price(symbol=target)
            price_str = mark_price_info.get('markPrice')
            if price_str:
                logger.debug(f"{log_ctx}: Precio de marca actual para {target}: {price_str}")
                return Decimal(price_str)
            else:
                logger.warning(f"{log_ctx}: No se pudo obtener 'markPrice' de la respuesta: {mark_price_info}")
                return None
        except BinanceAPIException as e:
            logger.error(f"{log_ctx}: Error API obteniendo precio de marca: {e}")
            return None
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado obteniendo precio de marca: {e}", exc_info=True)
            return None

    def place_market_order(self, side: str, quantity: Decimal, reduce_only: bool = False,
                           client_order_id: str = None, position_side_for_hedge: str = None,
                           symbol_target: str = None) -> dict | None:
        # ... (código existente sin cambios)
        target = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (PlaceMarketOrder - {target})"
        
        s_info = self.get_symbol_info_data(target)
        if not s_info:
            msg = "No se pudo obtener info del símbolo para orden MARKET."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_SYMBOL_INFO", "error_message": msg}

        adj_quantity = self.adjust_quantity_to_step_size(quantity, target)
        if not adj_quantity or adj_quantity <= Decimal(0): 
            msg = f"Cantidad ({quantity} -> {adj_quantity}) inválida tras ajuste para orden MARKET."
            logger.error(f"{log_ctx}: {msg}")
            return {"status": "ERROR_ADJUSTMENT", "error_message": msg}

        params = {
            "symbol": target, "side": side, "type": Client.ORDER_TYPE_MARKET,
            "quantity": self._format_order_quantity(adj_quantity, target),
        }

        if self.is_hedge_mode_active() and position_side_for_hedge:
            params['positionSide'] = position_side_for_hedge
            logger.info(f"{log_ctx}: Modo Cobertura. positionSide: {position_side_for_hedge}.")
        elif reduce_only:
            params["reduceOnly"] = "true" 
            logger.info(f"{log_ctx}: reduceOnly: true.")

        if client_order_id:
            params["newClientOrderId"] = client_order_id
            
        try:
            logger.info(f"{log_ctx}: Intentando orden MARKET: {params}")
            order_response = self.client.futures_create_order(**params)
            logger.info(f"{log_ctx}: Orden MARKET colocada. ID: {order_response.get('orderId')}, Status: {order_response.get('status')}")
            return order_response
        except (BinanceAPIException, BinanceOrderException) as e:
            logger.error(f"{log_ctx}: Error al colocar orden MARKET: {e}", exc_info=False)
            return {"status": "ERROR_API_ORDER", "error_message": str(e), "exception_code": e.code if hasattr(e, 'code') else None}
        except Exception as e:
            logger.error(f"{log_ctx}: Error inesperado al colocar orden MARKET: {e}", exc_info=True)
            return {"status": "ERROR_UNEXPECTED", "error_message": str(e)}

    def _cleanup_failed_bracket_orders(self, trade_details_dict, position_side_param_used):
        # ... (código existente sin cambios)
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (CleanupBracket - {self.symbol})"
        logger.info(f"{log_ctx}: Intentando limpiar órdenes de una operación parcialmente fallida.")

        position_side_for_hedge = None
        if self.is_hedge_mode_active() and trade_details_dict.get("entry_order_response"):
            entry_params = trade_details_dict["entry_order_response"] 
            if isinstance(entry_params, dict) and entry_params.get("positionSide"):
                position_side_for_hedge = entry_params.get("positionSide")

        entry_order_resp = trade_details_dict.get("entry_order_response")
        if entry_order_resp and isinstance(entry_order_resp, dict) and entry_order_resp.get("orderId"):
            entry_id = entry_order_resp.get("orderId")
            if not entry_order_resp.get("status", "").startswith("ERROR_"):
                logger.info(f"{log_ctx}: Intentando cancelar orden de entrada {entry_id} si está abierta.")
                self.cancel_order_if_open(order_id=entry_id, symbol_target=self.symbol, position_side_for_hedge=position_side_for_hedge)
        
        logger.info(f"{log_ctx}: Finalizado intento de limpieza de orden de entrada (si fue necesario).")

    def close_all_symbol_positions(self, symbol_to_close: str = None):
        # ... (código existente sin cambios)
        target_symbol = symbol_to_close.upper() if symbol_to_close else self.symbol
        logger.warning(f"({target_symbol}): Intentando cerrar TODAS las posiciones para {target_symbol} a mercado.")
        
        closed_any = False
        try:
            positions = self.client.futures_position_information(symbol=target_symbol)
            if not positions:
                logger.info(f"({target_symbol}): No hay información de posición, nada que cerrar.")
                return True 

            for position in positions:
                pos_amt_str = position.get('positionAmt', '0')
                pos_amt = Decimal(pos_amt_str)
                pos_side = position.get('positionSide') 

                if pos_amt != Decimal(0):
                    side_to_close = Client.SIDE_SELL if pos_amt > Decimal(0) else Client.SIDE_BUY
                    quantity_to_close = abs(pos_amt)
                    
                    logger.info(f"({target_symbol}): Cerrando posición {pos_side} de {quantity_to_close} {target_symbol} a mercado.")
                    close_order = self.place_market_order(
                        side=side_to_close,
                        quantity=quantity_to_close,
                        reduce_only=True, 
                        position_side_for_hedge=pos_side if pos_side in ["LONG", "SHORT"] else None,
                        symbol_target=target_symbol # Especificar el símbolo aquí
                    )
                    if close_order and close_order.get('orderId'):
                        logger.info(f"({target_symbol}): Posición {pos_side} cerrada con orden ID {close_order.get('orderId')}")
                        closed_any = True
                    else:
                        logger.error(f"({target_symbol}): Fallo al cerrar posición {pos_side}.")
                        return False 
            
            if not closed_any:
                logger.info(f"({target_symbol}): No se encontraron posiciones activas para cerrar.")
            return True

        except BinanceAPIException as e:
            logger.error(f"({target_symbol}): Error API al intentar cerrar todas las posiciones: {e}")
        except Exception as e:
            logger.error(f"({target_symbol}): Error inesperado al cerrar todas las posiciones: {e}", exc_info=True)
        return False

    def get_price_precision(self, symbol_target: str = None) -> int:
        # ... (código existente sin cambios)
        target_symbol = symbol_target.upper() if symbol_target else self.symbol
        s_info = self.get_symbol_info_data(target_symbol) 
        if not s_info:
            logger.warning(f"({target_symbol}): No hay symbol_info para determinar price_precision. Usando default de 2.")
            return 2

        if 'pricePrecision' in s_info: 
            return int(s_info['pricePrecision'])
        else: 
            tick_size_str = s_info.get('tickSize') 
            if isinstance(tick_size_str, Decimal): 
                tick_size_str = str(tick_size_str)

            if tick_size_str:
                if '.' in tick_size_str:
                    parts = tick_size_str.split('.')
                    if len(parts) > 1 and parts[1]:
                        return len(parts[1].rstrip('0'))
                    return 0
                else: 
                    return 0
            logger.warning(f"({target_symbol}): No se pudo determinar price_precision desde tickSize. Usando default de 2.")
            return 2
            
    # --- NUEVAS FUNCIONES AÑADIDAS ---
    def fetch_trade_closure_details(self, closing_order_id: int, original_entry_price: Decimal,
                                     position_quantity: Decimal, position_side: str, 
                                     symbol_target: str = None) -> dict | None:
        """
        Obtiene los detalles de cierre de un trade, incluyendo PnL.
        position_side: "LONG" o "SHORT" (de la posición original)
        """
        target_symbol_to_use = symbol_target.upper() if symbol_target else self.symbol
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (FetchClosureDetails - {target_symbol_to_use})"
        logger.info(f"{log_ctx}: Obteniendo detalles de cierre para orden {closing_order_id} en {target_symbol_to_use}")
        
        return get_trade_closure_details(
            binance_client=self.client,
            symbol=target_symbol_to_use,
            closing_order_id=closing_order_id,
            original_entry_price=original_entry_price,
            position_quantity=position_quantity,
            position_side=position_side 
        )

    def fetch_account_balance(self, asset: str = "USDT") -> Decimal | None:
        """Obtiene el balance de un activo específico de la cuenta de futuros."""
        log_ctx = f"{LOG_PREFIX_TRADE_MANAGER} (FetchAccountBalance - {asset})"
        logger.info(f"{log_ctx}: Obteniendo balance de {asset}")
        return get_futures_account_balance(binance_client=self.client, asset=asset.upper())
    # --- FIN DE NUEVAS FUNCIONES ---