import logging
import pandas as pd
from datetime import datetime, timedelta, timezone
import pytz
from binance.client import Client

# Asumiendo que bb_utils.py y config.py están en la misma carpeta o en el PYTHONPATH
from bb_utils import get_latest_5m_bollinger_bands_data
import config as cfg # Usaremos las configuraciones de config.py

# --- Configuración del Logging ---
# Esto asegurará que los logs de bb_utils y de este script se muestren.
logging.basicConfig(level=cfg.LOG_LEVEL,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler()]) # Imprimir en consola
logger = logging.getLogger(__name__)

def log_bb_and_1m_details():
    # --- Inicialización del Cliente y Zona Horaria ---
    try:
        client = Client(api_key=cfg.API_KEY, api_secret=cfg.API_SECRET)
        # Test de conexión (opcional, pero bueno para verificar credenciales si son necesarias)
        client.futures_ping()
        logger.info("Conexión exitosa con Binance Futures API.")
    except Exception as e:
        logger.error(f"Error al inicializar el cliente de Binance o hacer ping: {e}")
        return

    try:
        local_tz = pytz.timezone(cfg.LOCAL_TIMEZONE_STR_CFG)
    except pytz.exceptions.UnknownTimeZoneError:
        logger.error(f"Zona horaria '{cfg.LOCAL_TIMEZONE_STR_CFG}' desconocida. Usando UTC.")
        local_tz = pytz.utc

    # --- 1. Obtener y Loguear Datos de Bandas de Bollinger de 5 Minutos (Última Vela Completada) ---
    logger.info(f"Obteniendo datos de Bandas de Bollinger de 5M para {cfg.SYMBOL_CFG}...")
    bb_data_5m = get_latest_5m_bollinger_bands_data(
        client=client,
        symbol=cfg.SYMBOL_CFG,
        interval_5m=cfg.INTERVAL_5M_CFG,
        ma_type_pine=cfg.MA_TYPE_CFG,
        length=cfg.LENGTH_CFG,
        mult_orig=cfg.MULT_ORIG_CFG,
        mult_new=cfg.MULT_NEW_CFG,
        data_fetch_limit=100 # Un límite razonable para asegurar suficientes datos para el cálculo de BB
    )

    if not bb_data_5m or bb_data_5m.get("error"):
        logger.error(f"No se pudieron obtener los datos de BB de 5M. Mensaje: {bb_data_5m.get('message', 'Error desconocido')}")
        return

    logger.info("--- Datos de Bandas de Bollinger de 5 Minutos (Última Vela COMPLETADA) ---")
    logger.info(f"  Símbolo: {cfg.SYMBOL_CFG}")
    logger.info(f"  Timestamp (UTC): {bb_data_5m['timestamp_utc_iso']}")
    logger.info(f"  Low (vela 5M): {bb_data_5m['low']}")
    logger.info(f"  High (vela 5M): {bb_data_5m['high']}")
    logger.info(f"  BBM_orig ({cfg.MA_TYPE_CFG}{cfg.LENGTH_CFG}, {cfg.MULT_ORIG_CFG}): {bb_data_5m['BBM_orig']}")
    logger.info(f"  BBL_orig ({cfg.MULT_ORIG_CFG}): {bb_data_5m['BBL_orig']}")
    logger.info(f"  BBU_orig ({cfg.MULT_ORIG_CFG}): {bb_data_5m['BBU_orig']}")
    logger.info(f"  BBL_new ({cfg.MULT_NEW_CFG}): {bb_data_5m['BBL_new']}")
    logger.info(f"  BBU_new ({cfg.MULT_NEW_CFG}): {bb_data_5m['BBU_new']}")

    # --- 2. Determinar la Ventana de la Vela Actual de 5 Minutos ---
    # El timestamp de bb_data_5m es el inicio de la última vela *completada* de 5m.
    try:
        # Asegurarse de que el timestamp es timezone-aware (UTC)
        last_completed_5m_candle_open_time_utc = datetime.fromisoformat(bb_data_5m['timestamp_utc_iso'].replace('Z', '+00:00'))
    except Exception as e:
        logger.error(f"Error al parsear el timestamp de la vela de 5M: {e}")
        return

    current_5m_candle_start_time_utc = last_completed_5m_candle_open_time_utc + timedelta(minutes=5)
    next_5m_candle_start_time_utc = current_5m_candle_start_time_utc + timedelta(minutes=5)

    # Convertir a local para logging si es necesario, pero las operaciones de fetch usan UTC.
    current_5m_candle_start_local = current_5m_candle_start_time_utc.astimezone(local_tz)
    next_5m_candle_start_local = next_5m_candle_start_time_utc.astimezone(local_tz)

    captured_time_now_local = datetime.now(local_tz) # Tiempo actual para la lógica de obtención de velas de 1m

    logger.info(f"\n--- Velas de 1 Minuto para la Vela Actual de 5M en curso ({cfg.SYMBOL_CFG}) ---")
    logger.info(f"    (Intervalo 5M actual: {current_5m_candle_start_local.strftime('%Y-%m-%d %H:%M:%S %Z')} - {next_5m_candle_start_local.strftime('%Y-%m-%d %H:%M:%S %Z')})")

    # --- 3. Obtener y Loguear Datos de Velas de 1 Minuto para la Vela Actual de 5M ---
    # Esta lógica es similar a la de BB_buy.py/BB_sell.py
    # para obtener las velas de 1 minuto dentro de la vela actual de 5 minutos.
    
    # Asegurarse de que la vela actual de 5M ya ha comenzado y aún no ha terminado
    if captured_time_now_local.astimezone(pytz.utc) > current_5m_candle_start_time_utc and \
       captured_time_now_local.astimezone(pytz.utc) < next_5m_candle_start_time_utc:

        start_1m_fetch_utc_ms = int(current_5m_candle_start_time_utc.timestamp() * 1000)
        
        # Queremos velas de 1m hasta el minuto actual.
        # `captured_time_now_local` es el "ahora".
        # Para obtener todas las velas de 1m que han *abierto* hasta este momento dentro de la vela de 5m:
        end_1m_fetch_utc_ms = int(captured_time_now_local.astimezone(pytz.utc).timestamp() * 1000)

        # Si quieres replicar exactamente la lógica de BB_buy/BB_sell (velas completadas *antes* del minuto actual):
        # end_1m_fetch_limit_local_for_api = captured_time_now_local.replace(second=0, microsecond=0)
        # end_1m_fetch_utc_ms = int(end_1m_fetch_limit_local_for_api.astimezone(pytz.utc).timestamp() * 1000) -1 # Binance endTime es exclusivo

        if start_1m_fetch_utc_ms < end_1m_fetch_utc_ms : # Solo si hay un intervalo válido para buscar
            try:
                klines_1m = client.futures_klines(
                    symbol=cfg.SYMBOL_CFG,
                    interval=cfg.INTERVAL_1M_CFG,
                    startTime=start_1m_fetch_utc_ms,
                    endTime=end_1m_fetch_utc_ms # Obtiene velas cuya apertura es < endTime
                )
            except Exception as e:
                logger.error(f"  Error al obtener datos de 1M: {e}")
                klines_1m = []

            if klines_1m:
                cols_1m = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore']
                df_1m = pd.DataFrame(klines_1m, columns=cols_1m)

                # Convertir columnas numéricas y de timestamp
                numeric_cols_1m = ['open', 'high', 'low', 'close', 'volume']
                for col in numeric_cols_1m:
                    if col in df_1m.columns:
                        df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce')

                df_1m['timestamp_dt'] = pd.to_datetime(df_1m['timestamp'], unit='ms', utc=True).dt.tz_convert(local_tz)
                df_1m.set_index('timestamp_dt', inplace=True)
                df_1m.index.name = 'Timestamp_Local_1M'
                
                # Filtrar por si acaso la API devuelve velas fuera del rango exacto de inicio de 5M
                # (generalmente startTime se encarga de esto)
                df_1m_filtered = df_1m[df_1m.index >= current_5m_candle_start_local]


                if not df_1m_filtered.empty:
                    for ts_local, row_1m in df_1m_filtered.iterrows():
                        logger.info(f"  Vela 1M: {ts_local.strftime('%Y-%m-%d %H:%M:%S %Z')} - High: {row_1m['high']}, Low: {row_1m['low']}")
                else:
                    logger.info("  No se encontraron velas de 1 minuto completas para la vela actual de 5M (dataframe de 1M vacío después del filtro).")
            else:
                logger.info("  No se encontraron velas de 1 minuto para la vela actual de 5M (respuesta de API vacía). La vela de 5M puede haber comenzado recién.")
        else:
            logger.info("  La vela actual de 5M acaba de comenzar. Aún no hay intervalo para buscar velas de 1M.")
            
    elif captured_time_now_local.astimezone(pytz.utc) >= next_5m_candle_start_time_utc:
        logger.info(f"  La vela de 5M ({current_5m_candle_start_local.strftime('%H:%M')}-{next_5m_candle_start_local.strftime('%H:%M')}) ya ha finalizado. Ejecuta de nuevo en la siguiente vela de 5M.")
    else: # captured_time_now_local < current_5m_candle_start_time_utc (improbable si bb_data_5m es reciente)
        logger.info(f"  Aún no ha comenzado la siguiente vela de 5M esperada ({current_5m_candle_start_local.strftime('%H:%M')}). Hora actual: {captured_time_now_local.strftime('%H:%M:%S %Z')}")

if __name__ == "__main__":
    log_bb_and_1m_details()