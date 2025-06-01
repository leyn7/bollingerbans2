# Archivo: bb_utils.py
import pandas as pd
import pandas_ta as ta
import logging
from decimal import Decimal, InvalidOperation # InvalidOperation was not used, but Decimal is.
# from binance.client import Client # No longer needed directly here for kline fetching

# from websocket_data_provider import WebsocketDataProvider # For type hinting if desired

logger = logging.getLogger(__name__)

# INTERVAL_MAP_BB_UTILS is no longer needed here as get_latest_5m_bollinger_bands_data
# will receive string intervals and use WebsocketDataProvider.

def format_dataframe_for_print(df_input, float_cols, volume_cols, float_format="{:.2f}", volume_format="{:.0f}"):
    df_out = df_input.copy()
    for col in float_cols:
        if col in df_out.columns:
            df_out[col] = df_out[col].map(lambda x: float_format.format(x) if pd.notnull(x) else 'NaN')
    for col in volume_cols:
        if col in df_out.columns:
            df_out[col] = df_out[col].map(lambda x: volume_format.format(x) if pd.notnull(x) else 'NaN')
    return df_out

def calculate_signal_trigger(df_input,
                             event1_col,
                             reset_col,
                             price_event2_col,
                             band1_target_col,
                             band2_limit_col,
                             event2_logic_type="buy"):
    df = df_input.copy()
    signal_trigger_series = pd.Series([False] * len(df), index=df.index, name=f"signal_trigger_for_{event1_col}")
    last_event1_idx = None
    event2_achieved_for_this_e1 = False

    required_cols_for_calc = [event1_col, reset_col, price_event2_col, band1_target_col, band2_limit_col]
    if not all(col_check in df.columns for col_check in required_cols_for_calc):
        # logger.warning(...) # Consider if logging for missing columns is still desired
        return signal_trigger_series

    for idx, row in df.iterrows():
        current_signal_trigger = False
        current_reset_val = row[reset_col] if pd.notnull(row[reset_col]) else False
        current_event1_val = row[event1_col] if pd.notnull(row[event1_col]) else False

        if current_reset_val:
            last_event1_idx = None
            event2_achieved_for_this_e1 = False
        if not current_reset_val and current_event1_val:
            if last_event1_idx != idx:
                event2_achieved_for_this_e1 = False
            last_event1_idx = idx

        if last_event1_idx is not None and not event2_achieved_for_this_e1:
            current_price_e2 = row[price_event2_col]
            current_band1_target = row[band1_target_col]
            current_band2_limit = row[band2_limit_col]
            e2_condition_met_this_candle = False
            if pd.notnull(current_price_e2) and pd.notnull(current_band1_target) and pd.notnull(current_band2_limit):
                if event2_logic_type == "buy":
                    if current_price_e2 <= current_band1_target and current_price_e2 > current_band2_limit:
                        e2_condition_met_this_candle = True
                elif event2_logic_type == "sell":
                    if current_price_e2 >= current_band1_target and current_price_e2 < current_band2_limit:
                        e2_condition_met_this_candle = True
            if e2_condition_met_this_candle:
                event2_achieved_for_this_e1 = True
                current_signal_trigger = True
        signal_trigger_series.loc[idx] = current_signal_trigger
    return signal_trigger_series

def get_latest_5m_bollinger_bands_data(
    # client: Client, # No longer directly needed for kline fetching
    symbol: str,
    interval_5m_str: str, # Ej: "5m", "15m" (string for WDP)
    ma_type_pine: str,
    length: int,
    mult_orig: float,
    mult_new: float,
    websocket_data_provider # <--- NUEVO PARÁMETRO
):
    log_prefix = f"BB_UTILS_GET_LATEST ({symbol}@{interval_5m_str})"

    if websocket_data_provider is None:
        logger.error(f"{log_prefix}: WebsocketDataProvider no fue proporcionado.")
        return {"error": True, "message": "WebsocketDataProvider no disponible"}

    df_live = websocket_data_provider.get_dataframe(symbol, interval_5m_str)

    if df_live is None or df_live.empty:
        logger.warning(f"{log_prefix}: No se obtuvieron datos del WDP para {symbol} en {interval_5m_str}.")
        return {"error": True, "message": f"No hay datos de WDP para {symbol}@{interval_5m_str}"}
    
    if len(df_live) < length: # Necesitamos suficientes datos para el cálculo de la MA
        logger.warning(f"{log_prefix}: Datos insuficientes en WDP ({len(df_live)} velas) para {symbol} en {interval_5m_str} con longitud {length}.")
        return {"error": True, "message": f"Datos insuficientes de WDP ({len(df_live)} < {length})"}

    # Trabajar con una copia para no alterar el DataFrame del WDP
    df_calc = df_live.copy()

    ma_type_pd_ta = ma_type_pine.lower()

    bb_suffix_orig = f'_{length}_{str(mult_orig).replace(".", "_")}'
    bb_suffix_new = f'_{length}_{str(mult_new).replace(".", "_")}'

    bb_col_lower_orig = f'BBL{bb_suffix_orig}'
    bb_col_basis_orig = f'BBM{bb_suffix_orig}'
    bb_col_upper_orig = f'BBU{bb_suffix_orig}'

    bb_col_lower_new = f'BBL{bb_suffix_new}'
    bb_col_basis_new = f'BBM{bb_suffix_new}'
    bb_col_upper_new = f'BBU{bb_suffix_new}'

    try:
        df_calc.ta.bbands(close=df_calc['close'], length=length, std=mult_orig, mamode=ma_type_pd_ta, append=True,
                          col_names=(bb_col_lower_orig, bb_col_basis_orig, bb_col_upper_orig, f'BBB_temp_o{bb_suffix_orig}', f'BBP_temp_o{bb_suffix_orig}'))
        df_calc.ta.bbands(close=df_calc['close'], length=length, std=mult_new, mamode=ma_type_pd_ta, append=True,
                          col_names=(bb_col_lower_new, bb_col_basis_new, bb_col_upper_new, f'BBB_temp_n{bb_suffix_new}', f'BBP_temp_n{bb_suffix_new}'))
    except Exception as e_ta:
        logger.error(f"{log_prefix}: Error calculando Bandas de Bollinger con pandas_ta: {e_ta}", exc_info=True)
        return {"error": True, "message": f"Error en cálculo de BB: {e_ta}"}

    if len(df_calc) < 2: # Necesitamos al menos 2 filas para iloc[-2]
        logger.warning(f"{log_prefix}: DataFrame demasiado corto ({len(df_calc)}) después de calcular BBs.")
        return {"error": True, "message": "DataFrame corto post-cálculo BBs"}

    # Usar la penúltima vela, que es la última completada
    latest_completed_candle = df_calc.iloc[-2]

    required_cols_check = [
        bb_col_basis_orig, bb_col_lower_orig, bb_col_upper_orig,
        bb_col_lower_new, bb_col_upper_new,
        'low', 'high' # 'timestamp_dt' no está directamente en df_calc, el índice es el timestamp
    ]
    for r_col in required_cols_check:
        if r_col not in latest_completed_candle.index or pd.isna(latest_completed_candle[r_col]):
            logger.warning(f"{log_prefix}: Columna '{r_col}' no encontrada o es NaN en última vela completada.")
            return {"error": True, "message": f"Columna BB/dato faltante/NaN: {r_col}"}

    try:
        return {
            "error": False,
            "timestamp_utc_iso": latest_completed_candle.name.isoformat(), # .name es el índice (timestamp)
            "low": str(Decimal(str(latest_completed_candle['low']))),
            "high": str(Decimal(str(latest_completed_candle['high']))),
            "BBM_orig": str(Decimal(str(latest_completed_candle[bb_col_basis_orig]))),
            "BBL_orig": str(Decimal(str(latest_completed_candle[bb_col_lower_orig]))),
            "BBU_orig": str(Decimal(str(latest_completed_candle[bb_col_upper_orig]))),
            "BBL_new": str(Decimal(str(latest_completed_candle[bb_col_lower_new]))),
            "BBU_new": str(Decimal(str(latest_completed_candle[bb_col_upper_new]))),
        }
    except (InvalidOperation, TypeError, KeyError) as e_conv:
        logger.error(f"{log_prefix}: Error convirtiendo/accediendo a datos de vela para el retorno: {e_conv}", exc_info=True)
        return {"error": True, "message": f"Error en conversión de datos de vela: {e_conv}"}