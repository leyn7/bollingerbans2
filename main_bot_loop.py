import sys
import asyncio
import time
import logging
from datetime import datetime
import pytz
from decimal import getcontext, Decimal # Asegurar Decimal
import threading
import pandas as pd # No se usa directamente aquí, pero sí en módulos importados
import json
import os
from typing import Dict, Optional, Set, Any # Añadido para type hints

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException

import config # Importar config para LOG_LEVEL, API_KEYs, etc.
from binance_client_setup import BinanceClientSetup
from trade_manager import TradeManager
from persistent_state import PersistentState
from signal_processor import process_signals_and_initiate_trade
from pending_order_manager import manage_pending_order
from position_manager import manage_active_position
from telegram_manager import TelegramManager
from websocket_data_provider import WebsocketDataProvider
from bb_utils import get_latest_5m_bollinger_bands_data # Para el snapshot contextual

getcontext().prec = 18 # Precisión para Decimal

# --- Configuración de Event Loop para Windows (si es necesario) ---
if sys.platform == 'win32':
    print("MAIN_LOOP: Plataforma es Windows. Estableciendo WindowsSelectorEventLoopPolicy...", flush=True)
    try:
        selector_policy = asyncio.WindowsSelectorEventLoopPolicy()
        asyncio.set_event_loop_policy(selector_policy)
        # (Más logging de confirmación si es necesario)
    except Exception as e_policy_set:
        print(f"MAIN_LOOP: ERROR estableciendo política de event loop: {e_policy_set}", flush=True)

# --- Configuración de Logging ---
try:
    log_level_from_config = getattr(logging, config.LOG_LEVEL.upper(), logging.DEBUG)
except AttributeError:
    log_level_from_config = logging.DEBUG
log_file = getattr(config, 'LOG_FILE', 'trading_bot_activity.log')
logging.basicConfig(
    level=log_level_from_config,
    format='%(asctime)s.%(msecs)03d - %(levelname)-8s - %(name)-25s - [%(module)s.%(funcName)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, mode='a', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Mapeo de intervalos (puede estar en config.py o aquí)
LOCAL_CLIENT_INTERVAL_TO_STRING_MAP = {
    Client.KLINE_INTERVAL_1MINUTE: "1m",    
    Client.KLINE_INTERVAL_5MINUTE: "5m",
    Client.KLINE_INTERVAL_15MINUTE: "15m",
    
}
# Inverso para convertir constantes de cliente a string si es necesario
CLIENT_CONST_TO_STRING_MAP = {v: k for k, v in LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.items()}


# --- Funciones Auxiliares para Símbolos y Configuración ---
def _load_symbols_config_from_file() -> dict:
    """Carga la configuración de símbolos desde el archivo JSON."""
    filepath = getattr(config, 'SYMBOLS_CONFIG_FILE_PATH', 'symbols_config.json')
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"MAIN_LOOP: Error cargando {filepath}: {e}. Se intentará usar config por defecto o de Telegram.")
    return {}

def get_active_symbols_and_intervals(symbols_map: Dict[str, Dict[str, Any]]) -> Dict[str, Set[str]]:
    """
    Extrae los símbolos activos y todos los intervalos necesarios para el WDP.
    symbols_map es un diccionario como { "BTCUSDT": {"interval_5m": "5m", "interval_1m": "1m", "active": True, ...} }
    Devuelve: {"BTCUSDT": {"1m", "5m", "15m"}}
    """
    streams_to_manage: Dict[str, Set[str]] = {}
    if not isinstance(symbols_map, dict): # Asegurar que es un diccionario
        logger.warning("get_active_symbols_and_intervals: symbols_map no es un diccionario.")
        return streams_to_manage

    # Obtener el SL_REF_INTERVAL globalmente desde config.py
    # Asegurarse que es un string como "15m"
    sl_ref_interval_const = getattr(config, 'INTERVAL_15M_CFG', Client.KLINE_INTERVAL_15MINUTE)
    global_sl_ref_interval_str = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(sl_ref_interval_const, "15m")

    for symbol, params in symbols_map.items():
        if params.get("active", False): # Solo procesar símbolos activos
            intervals_for_symbol: Set[str] = set()
            
            # Intervalo principal (ej. 5m para señales)
            main_tf_const = params.get('interval_5m', config.INTERVAL_5M_CFG) # Puede ser constante o string
            main_tf_str = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(main_tf_const, str(main_tf_const) if isinstance(main_tf_const, str) else "5m")
            if main_tf_str: intervals_for_symbol.add(main_tf_str)

            # Intervalo de trigger/display (ej. 1m)
            trigger_tf_const = params.get('interval_1m', config.INTERVAL_1M_CFG) # Puede ser constante o string
            trigger_tf_str = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(trigger_tf_const, str(trigger_tf_const) if isinstance(trigger_tf_const, str) else "1m")
            if trigger_tf_str: intervals_for_symbol.add(trigger_tf_str)
            
            # Intervalo de referencia para SL (ej. 15m, globalmente definido)
            if global_sl_ref_interval_str: intervals_for_symbol.add(global_sl_ref_interval_str)
            
            if intervals_for_symbol:
                streams_to_manage[symbol.upper()] = intervals_for_symbol
            else:
                logger.warning(f"MAIN_LOOP: No se pudieron determinar intervalos para el símbolo activo {symbol}")
                
    return streams_to_manage

def _reload_config_and_update_symbols_map(current_config: dict, telegram_manager: Optional[TelegramManager]) -> dict:
    """Recarga la configuración de símbolos desde el archivo y la fusiona con la de Telegram si es necesario."""
    logger.info("MAIN_LOOP: Recargando configuración de símbolos...")
    file_config = _load_symbols_config_from_file()
    
    if telegram_manager and hasattr(telegram_manager, 'active_trading_symbols_config'):
        # Si la configuración del archivo es más reciente o si Telegram no tiene nada, usar archivo.
        # Aquí podrías implementar una lógica de fusión más sofisticada si es necesario,
        # por ahora, si el archivo existe y tiene contenido, podría tener precedencia
        # o si el de telegram es más completo porque se acaba de modificar.
        # Simplificación: si el archivo existe, se usa. Si no, se intenta el de Telegram.
        if file_config:
            # Si el archivo tiene algo, y difiere de lo que Telegram tenía en memoria,
            # se podría actualizar TelegramManager. Por ahora, solo retornamos la config del archivo.
            if file_config != telegram_manager.active_trading_symbols_config:
                 logger.info("MAIN_LOOP: Configuración de símbolos en archivo difiere de la de Telegram. Usando archivo y actualizando Telegram.")
                 telegram_manager.active_trading_symbols_config = file_config.copy() # Actualizar la copia en memoria de Telegram
                 telegram_manager._save_symbols_config() # Re-guardar para asegurar que el archivo es la fuente de verdad
            return file_config
        else: # Archivo vacío o no existe, usar la de Telegram Manager si tiene algo
            logger.info("MAIN_LOOP: Archivo de config vacío/no encontrado, usando config de TelegramManager si existe.")
            return telegram_manager.active_trading_symbols_config.copy()
            
    return file_config if file_config else current_config # Fallback a la config actual si todo falla


# Globales para WDP y Telegram (se inicializan en run_bot)
websocket_data_provider: Optional[WebsocketDataProvider] = None
telegram_bot_manager: Optional[TelegramManager] = None
websocket_thread: Optional[threading.Thread] = None # Hilo para WDP

def run_bot():
    global telegram_bot_manager, websocket_data_provider, websocket_thread # Modificar globales
    logger.info("MAIN_LOOP: --- Iniciando Ejecución de run_bot() ---")

    try:
        local_tz = pytz.timezone(config.LOCAL_TIMEZONE_STR_CFG)
    except pytz.exceptions.UnknownTimeZoneError:
        local_tz = pytz.utc
        logger.warning(f"MAIN_LOOP: Zona horaria '{config.LOCAL_TIMEZONE_STR_CFG}' inválida. Usando UTC.")

    try:
        client_handler = BinanceClientSetup(config.API_KEY, config.API_SECRET)
        binance_client = client_handler.get_client()
        logger.info("MAIN_LOOP: Cliente Binance inicializado exitosamente.")
    except Exception as e:
        logger.critical(f"MAIN_LOOP: Error CRÍTICO inicializando Binance client: {e}. Bot se detiene.", exc_info=True)
        return

    bot_state = PersistentState() # Usa el filepath de config.py por defecto
    logger.info("MAIN_LOOP: PersistentState inicializado.")

    all_configured_symbols_map = _load_symbols_config_from_file()

    if config.TELEGRAM_ENABLED and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            telegram_bot_manager = TelegramManager(
                token=config.TELEGRAM_BOT_TOKEN,
                admin_chat_id=config.TELEGRAM_CHAT_ID,
                binance_client_instance=binance_client, # Para el comando /close_pos
                persistent_state_instance=bot_state     # Para el comando /close_pos
            )
            # Si el archivo de símbolos estaba vacío pero Telegram tenía una config cargada (de un run anterior)
            if not all_configured_symbols_map and telegram_bot_manager.active_trading_symbols_config:
                logger.info("MAIN_LOOP: symbols_config.json vacío/no encontrado, usando config de TelegramManager.")
                all_configured_symbols_map = telegram_bot_manager.active_trading_symbols_config.copy()
                telegram_bot_manager._save_symbols_config() # Guardar para consistencia si se cargó de memoria de TG
            elif all_configured_symbols_map: # Si cargamos del archivo, actualizar TG
                 telegram_bot_manager.active_trading_symbols_config = all_configured_symbols_map.copy()

            telegram_thread = threading.Thread(target=telegram_bot_manager.start_telegram_listener_thread, name="Thread-TelegramListener", daemon=True)
            telegram_thread.start()
            logger.info("MAIN_LOOP: TelegramManager inicializado e hilo listener iniciado.")
            time.sleep(2) 
        except Exception as e:
            logger.error(f"MAIN_LOOP: Error inicializando TelegramManager: {e}", exc_info=True)
            telegram_bot_manager = None
    else:
        logger.warning("MAIN_LOOP: Telegram está deshabilitado o Token/ChatID no están configurados.")

    # Si después de todo all_configured_symbols_map está vacío, crear uno por defecto
    if not all_configured_symbols_map:
        if hasattr(config, 'SYMBOL_CFG') and config.SYMBOL_CFG:
            logger.info(f"MAIN_LOOP: No hay símbolos en JSON ni en Telegram. Creando config por defecto para {config.SYMBOL_CFG}.")
            default_symbol = config.SYMBOL_CFG
            # Usar LOCAL_CLIENT_INTERVAL_TO_STRING_MAP definido globalmente en este archivo
            interval_map = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP
            all_configured_symbols_map = {
                default_symbol: {
                    "interval_5m": interval_map.get(config.INTERVAL_5M_CFG, "5m"),
                    "interval_1m": interval_map.get(config.INTERVAL_1M_CFG, "1m"),
                    "ma_type": config.MA_TYPE_CFG, "length": int(config.LENGTH_CFG),
                    "mult_orig": float(config.MULT_ORIG_CFG), "mult_new": float(config.MULT_NEW_CFG),
                    "data_limit_5m": int(config.DATA_LIMIT_5M_CFG),
                    "fixed_quantity": float(getattr(config, 'DEFAULT_FIXED_TRADE_QUANTITY', 0.1)), # Asegurar float
                    "leverage": int(config.DEFAULT_LEVERAGE_FUTURES_CFG), 
                    "active": True
                }
            }
            # Guardar esta configuración por defecto si se creó
            try:
                with open(getattr(config, 'SYMBOLS_CONFIG_FILE_PATH', 'symbols_config.json'), 'w', encoding='utf-8') as f:
                    json.dump(all_configured_symbols_map, f, indent=4)
                logger.info("MAIN_LOOP: Configuración de símbolo por defecto guardada.")
                if telegram_bot_manager: # Actualizar la config en memoria de Telegram si está activo
                    telegram_bot_manager.active_trading_symbols_config = all_configured_symbols_map.copy()
            except Exception as e_save_def:
                logger.error(f"MAIN_LOOP: Error guardando config de símbolo por defecto: {e_save_def}")
        else:
            logger.error("MAIN_LOOP: No hay símbolos configurados y SYMBOL_CFG no está definido en config.py. El bot no puede operar.")
            # Podrías decidir salir aquí o que el bot espere a que se añadan símbolos vía Telegram.


    # --- Inicialización del WebsocketDataProvider ---
    active_symbols_initially = {s: p for s, p in all_configured_symbols_map.items() if p.get("active", True)}
    initial_streams_for_wdp = get_active_symbols_and_intervals(active_symbols_initially) 

    logger.info(f"MAIN_LOOP: Configuración inicial de streams para WDP: {initial_streams_for_wdp}")
    websocket_data_provider = WebsocketDataProvider(binance_client, local_tz_str=config.LOCAL_TIMEZONE_STR_CFG) # Pasar client y local_tz_str
    logger.info("MAIN_LOOP: WebsocketDataProvider instanciado. Las suscripciones se manejarán en el bucle.")
    
    # --- FIN Inicialización WebsocketDataProvider ---

    logger.info("MAIN_LOOP: --- Bot listo para bucle principal ---")
    if telegram_bot_manager:
        telegram_bot_manager.send_message_to_admin_threadsafe("✅ Bot (Principal): Bucle INICIADO. Monitoreando...")
    
    is_bot_operational_logged_state = True # Para loguear cambios de estado del bot (on/off)
    last_config_reload_time = time.time() # Para recargar config periódicamente

    while True:
        try:
            current_loop_start_time = time.time()
            current_time_local = datetime.now(local_tz) # Hora local para este ciclo
            logger.info(f"--- Inicio de NUEVO ciclo @ {current_time_local.strftime('%Y-%m-%d %H:%M:%S')} ---")

            # 1. Recargar configuración de símbolos si es necesario
            # (por tiempo o por flag de TelegramManager si implementas esa flag)
            if current_loop_start_time - last_config_reload_time > 300: # Recargar cada 5 minutos
                 all_configured_symbols_map = _reload_config_and_update_symbols_map(
                     current_config=all_configured_symbols_map,
                     telegram_manager=telegram_bot_manager
                 )
                 last_config_reload_time = current_loop_start_time

            # 2. Determinar símbolos a procesar en este ciclo
            symbols_to_process_runtime = {
                s: p for s, p in all_configured_symbols_map.items() if p.get("active", True)
            }

            if not symbols_to_process_runtime:
                logger.info("MAIN_LOOP: No hay símbolos activos para procesar este ciclo. Durmiendo...")
                time.sleep(config.LOOP_SLEEP_SECONDS)
                continue
            
            # 3. ✅ Actualizar/Suscribir streams del WDP según la configuración actual de símbolos
            if websocket_data_provider:
                all_intervals_needed_this_cycle: Dict[str, Set[str]] = {}
                for sym, params_cfg_loop in symbols_to_process_runtime.items():
                    intervals_for_sym_loop = set()
                    main_tf_const_loop = params_cfg_loop.get('interval_5m', config.INTERVAL_5M_CFG)
                    main_tf_str_loop = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(main_tf_const_loop, str(main_tf_const_loop) if isinstance(main_tf_const_loop, str) else "5m")
                    if main_tf_str_loop: intervals_for_sym_loop.add(main_tf_str_loop)

                    trigger_tf_const_loop = params_cfg_loop.get('interval_1m', config.INTERVAL_1M_CFG)
                    trigger_tf_str_loop = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(trigger_tf_const_loop, str(trigger_tf_const_loop) if isinstance(trigger_tf_const_loop, str) else "1m")
                    if trigger_tf_str_loop: intervals_for_sym_loop.add(trigger_tf_str_loop)
                    
                    sl_ref_interval_const_loop = getattr(config, 'INTERVAL_15M_CFG', Client.KLINE_INTERVAL_15MINUTE)
                    global_sl_ref_interval_str_loop = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(sl_ref_interval_const_loop, "15m")
                    if global_sl_ref_interval_str_loop: intervals_for_sym_loop.add(global_sl_ref_interval_str_loop)
                    
                    if intervals_for_sym_loop:
                        all_intervals_needed_this_cycle[sym.upper()] = intervals_for_sym_loop
       
                
                logger.debug(f"MAIN_LOOP: Asegurando suscripciones WDP para: {list(all_intervals_needed_this_cycle.keys())}")
                for sym_wdp, intervals_set_wdp in all_intervals_needed_this_cycle.items():
                    params_for_sym_wdp = symbols_to_process_runtime.get(sym_wdp) # Obtener params completos
                    if not params_for_sym_wdp: continue

                    bb_params_for_stream_wdp = {
                        'ma_type': params_for_sym_wdp.get('ma_type', config.MA_TYPE_CFG),
                        'length': int(params_for_sym_wdp.get('length', config.LENGTH_CFG)),
                        'mult_orig': float(params_for_sym_wdp.get('mult_orig', config.MULT_ORIG_CFG)),
                        'mult_new': float(params_for_sym_wdp.get('mult_new', config.MULT_NEW_CFG))
                    }
                    data_limit_hist_wdp = int(params_for_sym_wdp.get('data_limit_5m', config.DATA_LIMIT_5M_CFG))

                    for interval_str_wdp in intervals_set_wdp:
                        # Solo suscribir si bb_params son relevantes (ej. para 5m y 15m, no para 1m de display)
                        # O si WDP en subscribe_to_kline_stream maneja bb_params=None
                        current_bb_params = bb_params_for_stream_wdp if interval_str_wdp != trigger_tf_str_loop else None # No BB para trigger_tf si es solo para precio
                        
                        # Ajuste: Pasar bb_params para todos los tf que los necesiten para contexto.
                        # El trigger_tf (1m) usualmente no necesita bb_params para contexto en WDP.
                        # El principal_tf (5m) y sl_ref_tf (15m) sí.
                        pass_bb_params_to_wdp = bb_params_for_stream_wdp if interval_str_wdp in [main_tf_str_loop, global_sl_ref_interval_str_loop] else None
                        
                        websocket_data_provider.subscribe_to_kline_stream(
                            symbol=sym_wdp,
                            interval=interval_str_wdp,
                            data_limit_historical=data_limit_hist_wdp, # O un data_limit específico por intervalo
                            bb_params=pass_bb_params_to_wdp
                        )
                time.sleep(1) # Pequeña pausa después de (re)suscripciones
            else:
                logger.error("MAIN_LOOP: websocket_data_provider no está disponible.")


            # 4. Chequear estado global del bot (Telegram)
            if telegram_bot_manager:
                bot_globally_enabled = telegram_bot_manager.is_bot_globally_enabled()
                if not bot_globally_enabled:
                    if is_bot_operational_logged_state: # Loguear solo una vez el cambio de estado
                        logger.info("MAIN_LOOP: Bot DESHABILITADO globalmente vía Telegram. Pausando nuevas operaciones.")
                        # telegram_bot_manager.send_message_to_admin_threadsafe("ℹ️ Bot DESHABILITADO globalmente.") # Opcional
                        is_bot_operational_logged_state = False
                    time.sleep(config.LOOP_SLEEP_SECONDS)
                    continue # Saltar el resto del ciclo si el bot está deshabilitado
                elif not is_bot_operational_logged_state: # Si estaba apagado y ahora está encendido
                    logger.info("MAIN_LOOP: Bot HABILITADO globalmente vía Telegram. Reanudando operaciones.")
                    # telegram_bot_manager.send_message_to_admin_threadsafe("ℹ️ Bot HABILITADO globalmente.") # Opcional
                    is_bot_operational_logged_state = True
            
            # 5. Iterar por los símbolos a procesar
            logger.info(f"MAIN_LOOP: Símbolos a procesar en este ciclo: {list(symbols_to_process_runtime.keys())}")
            for symbol, params_loop in symbols_to_process_runtime.items(): # params_loop son los symbol_params
                log_prefix_sym_loop = f"MAIN_LOOP ({symbol})"
                logger.debug(f"{log_prefix_sym_loop}: Procesando...")
                try:
                    trade_manager_for_symbol = TradeManager(binance_client, symbol)
                    symbol_leverage = int(params_loop.get('leverage', config.DEFAULT_LEVERAGE_FUTURES_CFG))
                    if not trade_manager_for_symbol.set_leverage(leverage=symbol_leverage, symbol=symbol):
                        logger.error(f"{log_prefix_sym_loop}: Fallo al configurar apalancamiento {symbol_leverage}x. Saltando símbolo.")
                        if telegram_bot_manager: telegram_bot_manager.send_message_to_admin_threadsafe(f"ERROR ⚠️ ({symbol}): Fallo apalancamiento {symbol_leverage}x.")
                        continue
                    
                    # --- Contextual Snapshot Logging ---
                    if config.ENABLE_CTX_SNAPSHOT_LOG and websocket_data_provider:
                        try:
                            log_ctx_prefix = f"CTX_SNAPSHOT ({symbol})" # 'symbol' está bien aquí
                            price_prec_log = config.PRICE_PRECISION_LOG_CTX

                            # --- Datos de Velas ---
                            # Intervalo Principal (ej. 5m)
                            # Usa params_loop aquí en lugar de params
                            primary_interval_cfg_val = params_loop.get('interval_5m', config.INTERVAL_5M_CFG)
                            # Usa LOCAL_CLIENT_INTERVAL_TO_STRING_MAP
                            interval_p_str_ctx = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(
                                primary_interval_cfg_val, 
                                str(primary_interval_cfg_val) if isinstance(primary_interval_cfg_val, str) and primary_interval_cfg_val in LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.values() else "5m"
                            )
                            if interval_p_str_ctx not in LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.values(): # Chequeo adicional
                                interval_p_str_ctx = '5m'
                            
                            df_p_ws = websocket_data_provider.get_dataframe(symbol, interval_p_str_ctx)
                            clp_str, chp_str, ckts_str = "N/A", "N/A", "N/A"
                            if df_p_ws is not None and not df_p_ws.empty:
                                last_c_p = df_p_ws.iloc[-1]
                                ckts_str = last_c_p.name.to_pydatetime().astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                                chp_str = f"{Decimal(str(last_c_p['high'])):.{price_prec_log}f}"
                                clp_str = f"{Decimal(str(last_c_p['low'])):.{price_prec_log}f}"
                            
                            # Intervalo de 1m
                            # Usa LOCAL_CLIENT_INTERVAL_TO_STRING_MAP
                            int_1m_str_ctx = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_1M_CFG, '1m')
                            df_1m_ws = websocket_data_provider.get_dataframe(symbol, int_1m_str_ctx)
                            cl1m_str, ch1m_str, ck1m_ts_str = "N/A", "N/A", "N/A"
                            if df_1m_ws is not None and not df_1m_ws.empty:
                                last_c_1m = df_1m_ws.iloc[-1]
                                ck1m_ts_str = last_c_1m.name.to_pydatetime().astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                                ch1m_str = f"{Decimal(str(last_c_1m['high'])):.{price_prec_log}f}"
                                cl1m_str = f"{Decimal(str(last_c_1m['low'])):.{price_prec_log}f}"

                            # --- Datos de Bandas de Bollinger ---
                            # Usa params_loop aquí
                            bb_ma_type_snap = params_loop.get('ma_type', config.MA_TYPE_CFG)
                            bb_length_snap = int(params_loop.get('length', config.LENGTH_CFG))
                            bb_mult_orig_snap = float(params_loop.get('mult_orig', config.MULT_ORIG_CFG))
                            bb_mult_new_snap = float(params_loop.get('mult_new', config.MULT_NEW_CFG))

                            # Bandas para el Intervalo Principal (usando interval_p_str_ctx)
                            bbl_p_fmt, bbm_p_fmt, bbu_p_fmt = "N/A", "N/A", "N/A"
                            ts_pb_str = ckts_str # Timestamp de la vela principal como referencia
                            
                            # Aquí get_latest_5m_bollinger_bands_data es de bb_utils y no usa WDP directamente
                            # sino que WDP se le pasa como argumento.
                            # Esta función es para el display, no para la lógica de señales críticas que ya usa WDP.
                            primary_bands_data = get_latest_5m_bollinger_bands_data(
                                symbol=symbol, interval_5m_str=interval_p_str_ctx, # Usa el string del intervalo principal
                                ma_type_pine=bb_ma_type_snap, length=bb_length_snap,
                                mult_orig=bb_mult_orig_snap, mult_new=bb_mult_new_snap,
                                websocket_data_provider=websocket_data_provider, # Pasa WDP aquí
                                # client=binance_client, # get_latest_5m_bollinger_bands_data ya no debería necesitar client directamente si usa WDP
                                # local_tz=local_tz # Pasa si la función lo requiere
                            )
                            if primary_bands_data and not primary_bands_data.get("error"):
                                try:
                                    if primary_bands_data.get('BBL_orig'): bbl_p_fmt = f"{Decimal(str(primary_bands_data['BBL_orig'])):.{price_prec_log}f}"
                                    if primary_bands_data.get('BBM_orig'): bbm_p_fmt = f"{Decimal(str(primary_bands_data['BBM_orig'])):.{price_prec_log}f}"
                                    if primary_bands_data.get('BBU_orig'): bbu_p_fmt = f"{Decimal(str(primary_bands_data['BBU_orig'])):.{price_prec_log}f}"
                                    ts_iso_pb = primary_bands_data.get('timestamp_utc_iso')
                                    if ts_iso_pb: ts_pb_str = datetime.fromisoformat(ts_iso_pb.replace("Z", "+00:00")).astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                                except Exception as e_fmt: 
                                    logger.debug(f"{log_ctx_prefix}: Error formateando bandas snapshot principal para display: {e_fmt}")
                            
                            # BBM para el Intervalo de 15m (SL de referencia)
                            # Usa LOCAL_CLIENT_INTERVAL_TO_STRING_MAP
                            int_15m_str_ctx = LOCAL_CLIENT_INTERVAL_TO_STRING_MAP.get(config.INTERVAL_15M_CFG, '15m')
                            df_15m_ws = websocket_data_provider.get_dataframe(symbol, int_15m_str_ctx)
                            bbm15m_fmt, ts_15m_bands_str = "N/A", "N/A"

                            if df_15m_ws is not None and not df_15m_ws.empty:
                                ts_15m_bands_str = df_15m_ws.iloc[-1].name.to_pydatetime().astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                            
                            # Parámetros de BB para 15m (pueden ser los mismos o específicos si los defines en symbol_params)
                            # Usa params_loop aquí
                            sl_bb_ma_type = params_loop.get('ma_type_sl_15m', bb_ma_type_snap) 
                            sl_bb_length = int(params_loop.get('length_sl_15m', bb_length_snap))
                            sl_bb_mult_orig = float(params_loop.get('mult_orig_sl_15m', bb_mult_orig_snap))
                            sl_bb_mult_new = float(params_loop.get('mult_new_sl_15m', bb_mult_new_snap))

                            data_15m_bands = get_latest_5m_bollinger_bands_data(
                                symbol=symbol, interval_5m_str=int_15m_str_ctx,
                                ma_type_pine=sl_bb_ma_type, length=sl_bb_length,
                                mult_orig=sl_bb_mult_orig, mult_new=sl_bb_mult_new,
                                websocket_data_provider=websocket_data_provider
                            )
                            if data_15m_bands and not data_15m_bands.get("error"):
                                try:
                                    if data_15m_bands.get('BBM_orig'): bbm15m_fmt = f"{Decimal(str(data_15m_bands['BBM_orig'])):.{price_prec_log}f}"
                                    ts_iso_15m = data_15m_bands.get('timestamp_utc_iso')
                                    if ts_iso_15m: ts_15m_bands_str = datetime.fromisoformat(ts_iso_15m.replace("Z", "+00:00")).astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')
                                except Exception as e_fmt: 
                                    logger.debug(f"{log_ctx_prefix}: Error formateando BBM15m snapshot para display: {e_fmt}")

                            logger.info(
                                f"{log_ctx_prefix}:\n"
                                f"  Vela WS {interval_p_str_ctx} (TS: {ckts_str}): L={clp_str}, H={chp_str}\n"
                                f"  Vela WS {int_1m_str_ctx} (TS: {ck1m_ts_str}): L={cl1m_str}, H={ch1m_str}\n"
                                f"  Bandas WS {interval_p_str_ctx} (TS: {ts_pb_str}): BBL={bbl_p_fmt}, BBM={bbm_p_fmt}, BBU={bbu_p_fmt}\n"
                                f"  Media WS {int_15m_str_ctx} (TS: {ts_15m_bands_str}): BBM_15m={bbm15m_fmt}"
                            )
                        except Exception as e_ctx_log_outer:
                            logger.error(f"MAIN_LOOP_CTX ({symbol}): Error en bloque de logging contextual general: {e_ctx_log_outer}", exc_info=True) # exc_info=True para ver el traceback del error

                            
                    # --- Gestión de trades existentes y nuevas señales ---
                    state_key_long = f"{symbol}_LONG"; state_key_short = f"{symbol}_SHORT"
                    active_long_trade = bot_state.get_active_trade(state_key_long)
                    active_short_trade = bot_state.get_active_trade(state_key_short)

                    if active_long_trade:
                        if active_long_trade.get("status") == "PENDING_DYNAMIC_LIMIT":
                            manage_pending_order(active_long_trade, state_key_long, bot_state, trade_manager_for_symbol, binance_client, current_time_local, symbol, params_loop, websocket_data_provider)
                        elif active_long_trade.get("status") == "POSITION_OPEN":
                            manage_active_position(active_long_trade, state_key_long, bot_state, trade_manager_for_symbol, symbol, params_loop, telegram_bot_manager, websocket_data_provider)
                    
                    if active_short_trade:
                        if active_short_trade.get("status") == "PENDING_DYNAMIC_LIMIT":
                            manage_pending_order(active_short_trade, state_key_short, bot_state, trade_manager_for_symbol, binance_client, current_time_local, symbol, params_loop, websocket_data_provider)
                        elif active_short_trade.get("status") == "POSITION_OPEN":
                            manage_active_position(active_short_trade, state_key_short, bot_state, trade_manager_for_symbol, symbol, params_loop, telegram_bot_manager, websocket_data_provider)
                    
                    # Procesar nuevas señales (si no hay trades activos o pendientes para ese lado)
                    process_signals_and_initiate_trade(
                        bot_state, trade_manager_for_symbol, binance_client, local_tz,
                        current_time_local, symbol, params_loop, telegram_bot_manager, websocket_data_provider
                    )

                except BinanceAPIException as e_api_sym:
                    logger.error(f"{log_prefix_sym_loop}: Error API Binance: {e_api_sym.code} {e_api_sym.message[:100]}", exc_info=False)
                    if telegram_bot_manager: telegram_bot_manager.send_message_to_admin_threadsafe(f"ERROR API ⛔ ({symbol}): {e_api_sym.code} {e_api_sym.message[:50]}")
                    if hasattr(e_api_sym, 'code') and e_api_sym.code == -1021: time.sleep(config.LOOP_SLEEP_SECONDS * 4) 
                except BinanceOrderException as e_ord_sym:
                    logger.error(f"{log_prefix_sym_loop}: Error de Orden Binance: {e_ord_sym.code} {e_ord_sym.message[:100]}", exc_info=False)
                    if telegram_bot_manager: telegram_bot_manager.send_message_to_admin_threadsafe(f"ERROR ORDEN ⛔ ({symbol}): {e_ord_sym.code} {e_ord_sym.message[:50]}")
                except Exception as e_sym_loop:
                    logger.error(f"{log_prefix_sym_loop}: Error INESPERADO procesando símbolo: {e_sym_loop}", exc_info=True)
                    if telegram_bot_manager: telegram_bot_manager.send_message_to_admin_threadsafe(f"ERROR INESPERADO ⛔ ({symbol}): {str(e_sym_loop)[:100]}")
                finally:
                    logger.debug(f"{log_prefix_sym_loop}: Fin de procesamiento.")

            # --- Fin del bucle por Símbolos ---

            loop_duration = time.time() - current_loop_start_time
            sleep_duration = max(0, config.LOOP_SLEEP_SECONDS - loop_duration)
            logger.info(f"MAIN_LOOP: Fin de ciclo. Duración: {loop_duration:.2f}s. Durmiendo por {sleep_duration:.2f}s.")
            if sleep_duration > 0:
                time.sleep(sleep_duration)

        except KeyboardInterrupt:
            logger.info("MAIN_LOOP: Detención manual del bot (KeyboardInterrupt en bucle principal).")
            raise # Re-lanzar para que el finally del script principal se ejecute
        except Exception as e_main_loop_unhandled:
            logger.critical(f"MAIN_LOOP: Excepción CRÍTICA NO MANEJADA en el bucle principal: {e_main_loop_unhandled}", exc_info=True)
            if telegram_bot_manager:
                telegram_bot_manager.send_message_to_admin_threadsafe(f"💥 ERROR CRÍTICO Loop Principal: {str(e_main_loop_unhandled)[:100]}. Bot podría estar inestable.")
            time.sleep(60) # Esperar antes de reintentar


if __name__ == "__main__":
    logger.info("MAIN_SCRIPT: --- Script Iniciado (desde __main__) ---")
    try:
        if not config.API_KEY or not config.API_SECRET: # Chequeo básico de API Keys
            logger.critical("MAIN_SCRIPT: Claves API no configuradas o vacías en config.py. Abortando.")
        else:
            logger.info("MAIN_SCRIPT: Llamando a run_bot()...")
            run_bot()
    except KeyboardInterrupt:
        logger.info("MAIN_SCRIPT: Bot detenido manualmente (KeyboardInterrupt detectado en __main__).")
        # El apagado de WDP y Telegram se maneja en el finally general
    except Exception as e_top_level:
        logger.critical(f"MAIN_SCRIPT: Error CRÍTICO de nivel superior no capturado en run_bot() o durante inicialización: {e_top_level}", exc_info=True)
        if telegram_bot_manager: # Intentar notificar si TG Manager se inicializó
            telegram_bot_manager.send_message_to_admin_threadsafe(f"💥 ERROR CRÍTICO Global: {str(e_top_level)[:100]}. Bot detenido.")
    finally:
        logger.info("MAIN_SCRIPT: --- Inicio del bloque FINALLY principal de __main__ (limpieza) ---")
        if websocket_data_provider and hasattr(websocket_data_provider, 'stop_all_streams'):
            logger.info("MAIN_SCRIPT (finally): Asegurando que todos los streams del WebsocketDataProvider están detenidos...")
            websocket_data_provider.stop_all_streams()
        else:
            logger.info("MAIN_SCRIPT (finally): WebsocketDataProvider no fue instanciado o ya fue limpiado/no tiene stop_all_streams.")
        
        if telegram_bot_manager and hasattr(telegram_bot_manager, 'shutdown_event') and telegram_bot_manager.shutdown_event:
            if telegram_bot_manager.telegram_loop and not telegram_bot_manager.telegram_loop.is_closed():
                logger.info("MAIN_SCRIPT (finally): Señalando al hilo de Telegram para que termine (si está activo)...")
                # Asegurarse de que shutdown_event es un asyncio.Event y el loop está activo
                if isinstance(telegram_bot_manager.shutdown_event, asyncio.Event):
                     telegram_bot_manager.telegram_loop.call_soon_threadsafe(telegram_bot_manager.shutdown_event.set)
                
                # Esperar al hilo de Telegram
                # Localizar el hilo por nombre (asumiendo que se nombró consistentemente)
                tg_thread_final_check = next((t for t in threading.enumerate() if t.name == "Thread-TelegramListener"), None)
                if tg_thread_final_check and tg_thread_final_check.is_alive():
                    logger.info("MAIN_SCRIPT (finally): Esperando que el hilo de Telegram termine (timeout 10s)...")
                    tg_thread_final_check.join(timeout=10)
                    if tg_thread_final_check.is_alive():
                        logger.warning("MAIN_SCRIPT (finally): El hilo de Telegram no terminó en el tiempo esperado.")
                    else:
                        logger.info("MAIN_SCRIPT (finally): Hilo de Telegram terminado.")
                else:
                    logger.info("MAIN_SCRIPT (finally): Hilo de Telegram no encontrado o ya no estaba activo al momento del chequeo final.")
            else:
                logger.info("MAIN_SCRIPT (finally): Bucle de Telegram no activo o ya cerrado, no se puede señalar para apagar.")
        else:
            logger.info("MAIN_SCRIPT (finally): TelegramManager no inicializado o sin evento de apagado configurado.")
            
        logger.info("MAIN_SCRIPT: --- FIN DEL SCRIPT ---")