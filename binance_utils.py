# Archivo: binance_utils.py

import logging
from decimal import Decimal, ROUND_HALF_UP # Para precisión financiera
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

logger = logging.getLogger(__name__)

def get_futures_account_balance(binance_client: Client, asset: str = "USDT") -> Decimal | None:
    
    logger_prefix = f"GET_BALANCE_UTIL({asset})"
    try:
        balances = binance_client.futures_account_balance()
        for balance_info in balances:
            if balance_info['asset'].upper() == asset.upper():
                # 'balance' usualmente es el total incluyendo PnL no realizado de posiciones abiertas.
                total_balance = Decimal(str(balance_info['balance']))
                logger.info(f"{logger_prefix}: Balance encontrado: {total_balance} {asset}")
                return total_balance
        logger.warning(f"{logger_prefix}: Activo no encontrado en los balances de futuros.")
        return None
    except BinanceAPIException as e:
        logger.error(f"{logger_prefix}: Error de API de Binance al obtener balance: {e}")
        return None
    except Exception as e:
        logger.error(f"{logger_prefix}: Error inesperado al obtener balance: {e}", exc_info=True)
        return None

def get_trade_closure_details(
    binance_client: Client,
    symbol: str,
    closing_order_id: int,
    original_entry_price: Decimal, # Precio de entrada original de la posición
    position_quantity: Decimal,    # Cantidad de la posición (positiva)
    position_side: str             # "LONG" o "SHORT"
) -> dict | None:
    
    logger_prefix = f"TRADE_CLOSURE_UTIL({symbol}, OrdID:{closing_order_id})"
    try:
        # 1. Obtener detalles de la orden de cierre para confirmar estado y obtener tiempo de ejecución
        order_details = binance_client.futures_get_order(symbol=symbol, orderId=closing_order_id)
        
        if order_details['status'] != 'FILLED':
            logger.warning(f"{logger_prefix}: La orden de cierre no está FILLED. Estado actual: {order_details['status']}")
            return None # No procesar si la orden no se ha llenado

        order_update_time_ms = int(order_details['updateTime']) # Timestamp de la última actualización (ejecución)
        avg_price_from_order = Decimal(str(order_details.get('avgPrice', '0'))) # Precio promedio de la orden
        executed_qty_from_order = Decimal(str(order_details.get('executedQty', '0'))) # Cantidad ejecutada de la orden

        if executed_qty_from_order == Decimal('0'):
            logger.warning(f"{logger_prefix}: La orden de cierre {closing_order_id} tiene cantidad ejecutada cero.")
            return None

        # 2. Buscar los trades (fills) asociados a esta orden de cierre
        start_time_ms = order_update_time_ms - 60000  # 1 minuto antes
        end_time_ms = order_update_time_ms + 300000    # 5 minutos después (por si hay latencia en los fills)

        account_trades = binance_client.futures_account_trades(
            symbol=symbol, 
            startTime=start_time_ms, 
            endTime=end_time_ms, 
            limit=100 
        )

        relevant_trades = [
            trade for trade in account_trades 
            if int(trade['orderId']) == closing_order_id
        ]

        if not relevant_trades:
            logger.warning(f"{logger_prefix}: No se encontraron trades (fills) para la orden de cierre {closing_order_id} en el rango de tiempo.")
            # Fallback: Calcular PnL manualmente usando el precio promedio de la orden.
            closed_quantity_manual = executed_qty_from_order
            avg_close_price_manual = avg_price_from_order
            pnl_manual = Decimal('0.0')

            if position_side.upper() == "LONG":
                pnl_manual = (avg_close_price_manual - original_entry_price) * closed_quantity_manual
            elif position_side.upper() == "SHORT":
                pnl_manual = (original_entry_price - avg_close_price_manual) * closed_quantity_manual
            
            logger.info(f"{logger_prefix}: Usando PnL calculado manualmente (sin comisiones explícitas): {pnl_manual:.8f}")
            
            # Determinar el exponente para la cuantización basado en original_entry_price
            if original_entry_price.as_tuple().exponent < 0:
                quantizer_manual = Decimal('1e' + str(original_entry_price.as_tuple().exponent))
            else:
                quantizer_manual = Decimal('1')

            return {
                "realized_pnl": pnl_manual,
                "avg_close_price": avg_close_price_manual.quantize(quantizer_manual, rounding=ROUND_HALF_UP),
                "closed_quantity": closed_quantity_manual,
                "commission": Decimal("0"), 
                "commission_asset": "USDT", 
                "close_time_utc_ms": order_update_time_ms
            }

        # Procesar los trades encontrados
        total_realized_pnl = Decimal("0")
        total_commission = Decimal("0")
        total_qty_closed_from_trades = Decimal("0")
        weighted_sum_price_qty = Decimal("0")
        commission_asset = ""
        last_trade_time_ms = 0

        for trade in relevant_trades:
            pnl_trade = Decimal(str(trade['realizedPnl']))     
            commission_trade = Decimal(str(trade['commission']))
            qty_trade = Decimal(str(trade['qty']))             
            price_trade = Decimal(str(trade['price']))          
            
            total_realized_pnl += pnl_trade
            total_commission += commission_trade
            total_qty_closed_from_trades += qty_trade
            weighted_sum_price_qty += price_trade * qty_trade
            
            if not commission_asset and trade.get('commissionAsset'):
                commission_asset = trade['commissionAsset']
            elif commission_asset and trade.get('commissionAsset') and commission_asset != trade['commissionAsset']:
                logger.warning(f"{logger_prefix}: Múltiples activos de comisión encontrados: {commission_asset} y {trade['commissionAsset']}")
            
            last_trade_time_ms = max(last_trade_time_ms, int(trade['time']))

        if total_qty_closed_from_trades == Decimal("0"):
            logger.warning(f"{logger_prefix}: La cantidad total cerrada de los trades es cero, aunque se encontraron {len(relevant_trades)} trades.")
            return None 

        avg_close_price_from_trades = weighted_sum_price_qty / total_qty_closed_from_trades
        
        logger.info(f"{logger_prefix}: Detalles de cierre (desde trades): PnL={total_realized_pnl:.8f}, AvgPriceRaw={avg_close_price_from_trades}, Qty={total_qty_closed_from_trades}")
        
        # --- INICIO DE LA CORRECCIÓN ---
        # Determinar el exponente para la cuantización basado en original_entry_price
        if original_entry_price.as_tuple().exponent < 0:
            quantizer = Decimal('1e' + str(original_entry_price.as_tuple().exponent))
        else:
            quantizer = Decimal('1') 
        # --- FIN DE LA CORRECCIÓN ---

        return {
            "realized_pnl": total_realized_pnl,
            "avg_close_price": avg_close_price_from_trades.quantize(quantizer, rounding=ROUND_HALF_UP), # CORRECCIÓN APLICADA
            "closed_quantity": total_qty_closed_from_trades,
            "commission": total_commission,
            "commission_asset": commission_asset if commission_asset else "USDT", 
            "close_time_utc_ms": last_trade_time_ms if last_trade_time_ms else order_update_time_ms
        }

    except BinanceAPIException as e:
        logger.error(f"{logger_prefix}: Error de API de Binance al obtener detalles de cierre: {e}")
        return None
    except BinanceOrderException as e: 
        logger.error(f"{logger_prefix}: Error de orden de Binance al obtener detalles de cierre: {e}")
        return None
    except Exception as e:
        logger.error(f"{logger_prefix}: Error inesperado al obtener detalles de cierre: {e}", exc_info=True)
        return None