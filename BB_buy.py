# Archivo: BB_buy.py
from typing import Dict, Optional, Any # Añadido Any
import pandas as pd
from binance.client import Client # Para constantes de Client.KLINE_INTERVAL_ si se usan en fallback
from datetime import datetime, timedelta
import pytz
from decimal import Decimal, InvalidOperation

import config # Asumiendo que existe y es accesible
from bb_utils import format_dataframe_for_print # Asumiendo que existe
from bb_signal_utils import fetch_and_prepare_bb_data # Sigue siendo útil para el display

import logging
logger = logging.getLogger(__name__)

def analizar_señales_compra(
    client: Client,                     # 1
    symbol: str,                        # 2
    interval_principal_str: str,        # 3
    interval_trigger_str: str,          # 4
    interval_sl_ref_str: str,           # 5
    trade_manager: Any,                 # 6
    websocket_data_provider: Any,       # 7
    local_tz: pytz.timezone,            # 8
    captured_time_local: datetime,      # 9
    ma_type_pine: str,                  # 10 (nombre de parámetro esperado por common_signal_params)
    length: int,                        # 11
    mult_orig: float,                   # 12
    mult_new: float,                    # 13
    data_limit_5m: int,                 # 14
    candles_to_print_5m: int,           # 15
    verbose: bool = True                # 16 (con valor por defecto)
) -> Optional[Dict[str, Any]]:

    log_prefix = f"BB_buy ({symbol})"
    if verbose:
        logger.info(f"{log_prefix}: Iniciando lógica de detección de señal de COMPRA.")

    # 1. Obtener el precio actual del intervalo de trigger (ej. 1m)
    df_trigger = websocket_data_provider.get_dataframe(symbol, interval_trigger_str)
    if df_trigger is None or df_trigger.empty or len(df_trigger) < 1:
        if verbose: logger.debug(f"{log_prefix}: No hay datos suficientes del DataFrame de '{interval_trigger_str}' para obtener precio actual.")
        return None
    
    last_trigger_candle = df_trigger.iloc[-1]
    current_price_trigger_interval = Decimal(str(last_trigger_candle['close']))
    timestamp_of_signal_utc = last_trigger_candle.name # pd.Timestamp UTC
    
    if verbose: logger.debug(f"{log_prefix}: Precio actual ({interval_trigger_str}) = {current_price_trigger_interval} @ {timestamp_of_signal_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # 2. Obtener valores de bandas contextuales del intervalo principal (ej. 5m)
    bands_main_dict = websocket_data_provider.get_contextual_bollinger_bands(symbol, interval_principal_str)
    if not bands_main_dict:
        if verbose: logger.debug(f"{log_prefix}: No se encontraron bandas de contexto para {symbol}@{interval_principal_str}.")
        return None
    
    try:
        bbl_orig_main = Decimal(str(bands_main_dict['BBL_orig']))
        bbm_orig_main = Decimal(str(bands_main_dict['BBM_orig'])) # BBM_orig del timeframe principal
        bbl_new_main = Decimal(str(bands_main_dict['BBL_new']))   # Este será el entry_price_target
    except (KeyError, InvalidOperation, TypeError) as e: # Añadido TypeError
        if verbose: logger.error(f"{log_prefix}: Error al obtener/convertir valores de bandas principales de WDP: {e}. Datos: {bands_main_dict}")
        return None
        
    if verbose: logger.debug(f"{log_prefix}: Bandas {interval_principal_str} (contexto): BBL_orig={bbl_orig_main}, BBM_orig={bbm_orig_main}, BBL_new(target_entry)={bbl_new_main}")

    # 3. Obtener BBM_orig del intervalo de referencia del SL (ej. 15m) -> Este es el stop_loss_price_ref
    bbm_sl_ref_val_str = websocket_data_provider.get_specific_contextual_band_value(symbol, interval_sl_ref_str, 'BBM_orig')
    if bbm_sl_ref_val_str is None: # get_specific_contextual_band_value ya devuelve Decimal o None
        if verbose: logger.debug(f"{log_prefix}: No se encontró BBM de contexto para {symbol}@{interval_sl_ref_str} (para SL de referencia).")
        return None
    
    # La función get_specific_contextual_band_value ya debería devolver Decimal, pero re-aseguramos
    try:
        bbm_sl_ref = Decimal(str(bbm_sl_ref_val_str))
    except (InvalidOperation, TypeError):
        if verbose: logger.error(f"{log_prefix}: Error convirtiendo BBM_sl_ref '{bbm_sl_ref_val_str}' a Decimal.")
        return None

    if verbose: logger.debug(f"{log_prefix}: BBM {interval_sl_ref_str} (contexto SL de referencia) = {bbm_sl_ref}")

    # 4. Verificar las Nuevas Condiciones para Compra
    signal_detected = False
    # Condición 1: BBL_orig_5m > BBM_15m
    pre_condition_met = bbl_orig_main > bbm_sl_ref
    # Condición 2: precio_actual_1m < BBM_orig_5m
    price_trigger_met = current_price_trigger_interval < bbm_orig_main

    if verbose:
        logger.debug(f"{log_prefix}: Chequeo COMPRA:")
        logger.debug(f"  Pre-condición (BBL_orig_{interval_principal_str} ({bbl_orig_main}) > BBM_{interval_sl_ref_str} ({bbm_sl_ref})) = {pre_condition_met}")
        logger.debug(f"  Disparador Precio (Precio_{interval_trigger_str} ({current_price_trigger_interval}) < BBM_orig_{interval_principal_str} ({bbm_orig_main})) = {price_trigger_met}")

    if pre_condition_met and price_trigger_met:
        logger.info(f"{log_prefix}: ¡CONDICIONES CRUDAS DE SEÑAL DE COMPRA DETECTADAS para {symbol} @ {current_price_trigger_interval} (vela {interval_trigger_str})!")
        signal_detected = True
    
    # Devolver componentes crudos si la señal es detectada
    if signal_detected:
        raw_signal_components = {
            "signal_detected": True,
            "entry_price_target": bbl_new_main, # Este es el precio de entrada objetivo
            "stop_loss_price_ref": bbm_sl_ref,  # Este es el SL de referencia (BBM del timeframe de SL)
            "timestamp_of_signal_utc": timestamp_of_signal_utc,
            "signal_type": "COMPRA",
            # Puedes añadir otros datos que `signal_processor` podría necesitar para logging o decisiones:
            "current_price_trigger_interval": current_price_trigger_interval,
            "bbm_orig_main_tf": bbm_orig_main, # BBM del timeframe principal
            "bbl_orig_main_tf": bbl_orig_main  # BBL_orig del timeframe principal
        }
        # Ya NO se llama a validate_and_calculate_trade_params aquí.
        # Eso se hará en signal_processor.py.
        
        # --- Lógica de Visualización (Verbose Mode) ---
        # Esta parte puede seguir existiendo para mostrar el contexto de la señal detectada
        if verbose:
            logger.info(f"{log_prefix}: Mostrando contexto de datos para señal COMPRA detectada (antes de validación final).")
            # (El código de display que tenías puede ir aquí, pero ten en cuenta que
            #  ya no tendrás 'most_recent_valid_signal' con E, SL, TP finales en este punto)
            # Ejemplo simplificado de lo que podrías loguear/imprimir:
            df_5m_display_with_bands = None
            if websocket_data_provider:
                df_5m_base_for_display = websocket_data_provider.get_dataframe(symbol, interval_principal_str)
                if df_5m_base_for_display is not None and not df_5m_base_for_display.empty:
                    df_5m_display_with_bands = fetch_and_prepare_bb_data(
                        client=client, symbol=symbol, interval_5m=interval_principal_str, 
                        length=length, mult_orig=mult_orig, mult_new=mult_new, ma_type_pine=ma_type_pine,
                        data_limit_5m=len(df_5m_base_for_display), 
                        local_tz=local_tz, verbose=False,
                        websocket_data_provider=websocket_data_provider # Pasar WDP
                    )
            
            if df_5m_display_with_bands is not None and not df_5m_display_with_bands.empty:
                # ... (tu lógica de formateo e impresión del DataFrame de 5m como la tenías) ...
                # Solo recuerda que aquí no tienes los E, SL, TP finales aún.
                # Puedes imprimir los valores de las bandas, el precio de entrada objetivo, el SL de referencia.
                # Por brevedad, omito el código de impresión detallado aquí.
                # Ejemplo:
                print(f"\n--- ({log_prefix}) Contexto Señal COMPRA {interval_principal_str} ({symbol}) ---")
                print(f"  Entrada Objetivo: {raw_signal_components['entry_price_target']}")
                print(f"  SL Referencia   : {raw_signal_components['stop_loss_price_ref']}")
                print(f"  Precio Trigger  : {raw_signal_components['current_price_trigger_interval']}")
                # Y luego la tabla de velas si quieres...
            else:
                print(f" ({log_prefix}) No se pudo obtener/preparar DF de {interval_principal_str} para display.")
            print("-" * 180)
            # La lógica de display de 1 minuto también puede permanecer si es útil.

        return raw_signal_components # Devuelve el diccionario con componentes crudos

    # Si no se detectó señal
    if verbose:
        logger.debug(f"{log_prefix}: No se detectaron condiciones de señal de COMPRA en este ciclo.")
    
    # La parte de visualización si no hay señal (si tenías algo específico)
    if verbose:
        # ... (código de display si no hubo señal, similar al de arriba pero indicando "sin señal")
        # Por ejemplo, solo imprimir la tabla de 5m para contexto general.
        df_5m_display_with_bands = None
        if websocket_data_provider:
            df_5m_base_for_display = websocket_data_provider.get_dataframe(symbol, interval_principal_str)
            if df_5m_base_for_display is not None and not df_5m_base_for_display.empty:
                df_5m_display_with_bands = fetch_and_prepare_bb_data(
                    client=client, symbol=symbol, interval_5m=interval_principal_str, 
                    length=length, mult_orig=mult_orig, mult_new=mult_new, ma_type_pine=ma_type_pine,
                    data_limit_5m=len(df_5m_base_for_display), 
                    local_tz=local_tz, verbose=False,
                    websocket_data_provider=websocket_data_provider
                )
        if df_5m_display_with_bands is not None and not df_5m_display_with_bands.empty:
            # ... (tu lógica de formateo e impresión del DataFrame de 5m) ...
            pass # Omitido por brevedad
        # ... (display de 1m si es relevante) ...
        print("-" * 180)

    return None # Devuelve None si no hay señal