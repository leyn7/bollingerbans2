# Archivo: BB_strategy.py
from binance.client import Client
from datetime import datetime, timezone # Asegurar que timezone esté aquí también
import pytz

from BB_buy import analizar_señales_compra
from BB_sell import analizar_señales_venta

# --- Configuración Principal de la Estrategia ---
SYMBOL_CFG = "BNBUSDT"
INTERVAL_5M_CFG = Client.KLINE_INTERVAL_5MINUTE
INTERVAL_1M_CFG = Client.KLINE_INTERVAL_1MINUTE # Si se usa para visualización en los módulos
MA_TYPE_CFG = "SMA"
LENGTH_CFG = 20
MULT_ORIG_CFG = 2.0
MULT_NEW_CFG = 1.0
DATA_LIMIT_5M_CFG = 300
CANDLES_TO_PRINT_CFG = 10 # Cuántas velas mostrará BB_strategy si decide imprimir tablas
LOCAL_TIMEZONE_STR_CFG = 'America/Bogota'

# --- Inicialización ---
try:
    local_tz_obj = pytz.timezone(LOCAL_TIMEZONE_STR_CFG)
except pytz.exceptions.UnknownTimeZoneError:
    print(f"Error: Zona horaria '{LOCAL_TIMEZONE_STR_CFG}' desconocida. Usando UTC como fallback.")
    local_tz_obj = pytz.utc

client_obj = Client()

def run_strategy():
    print(f"--- Iniciando Estrategia BB para {SYMBOL_CFG} ---")
    current_time = datetime.now(local_tz_obj)
    print(f"Hora actual: {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # --- Obtener Señal de Compra ---
    # verbose=False para que BB_buy no imprima sus tablas detalladas, solo devuelva la señal.
    # BB_strategy imprimirá su propio resumen.
    print("\nAnalizando señal de COMPRA...")
    buy_signal = analizar_señales_compra(
        client=client_obj, 
        symbol=SYMBOL_CFG, 
        interval_5m=INTERVAL_5M_CFG, 
        interval_1m=INTERVAL_1M_CFG, 
        ma_type_pine=MA_TYPE_CFG, 
        length=LENGTH_CFG,
        mult_orig=MULT_ORIG_CFG, 
        mult_new=MULT_NEW_CFG, 
        data_limit_5m=DATA_LIMIT_5M_CFG, 
        candles_to_print_5m=CANDLES_TO_PRINT_CFG, # Cuántas velas procesar para la tabla en el módulo
        local_tz=local_tz_obj, 
        captured_time_local=current_time, 
        verbose=False # Cambiar a True si quieres ver las tablas de BB_buy.py
    )

    # --- Obtener Señal de Venta ---
    print("\nAnalizando señal de VENTA...")
    sell_signal = analizar_señales_venta(
        client=client_obj, 
        symbol=SYMBOL_CFG, 
        interval_5m=INTERVAL_5M_CFG, 
        interval_1m=INTERVAL_1M_CFG, 
        ma_type_pine=MA_TYPE_CFG, 
        length=LENGTH_CFG,
        mult_orig=MULT_ORIG_CFG, 
        mult_new=MULT_NEW_CFG, 
        data_limit_5m=DATA_LIMIT_5M_CFG, 
        candles_to_print_5m=CANDLES_TO_PRINT_CFG,
        local_tz=local_tz_obj, 
        captured_time_local=current_time, 
        verbose=False # Cambiar a True si quieres ver las tablas de BB_sell.py
    )

    # --- Resumen y Decisión (Placeholder) ---
    print("\n--- Resumen de la Estrategia BB ---")
    if buy_signal:
        print(f"ALERTA DE COMPRA:")
        print(f"  Timestamp: {buy_signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Entrada: {buy_signal['entry']:.2f}, SL: {buy_signal['sl']:.2f}, TP: {buy_signal['tp']:.2f}")
    else:
        print("No hay señal de COMPRA válida activa.")

    if sell_signal:
        print(f"ALERTA DE VENTA:")
        print(f"  Timestamp: {sell_signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Entrada: {sell_signal['entry']:.2f}, SL: {sell_signal['sl']:.2f}, TP: {sell_signal['tp']:.2f}")
    else:
        print("No hay señal de VENTA válida activa.")
    
    # Aquí iría la lógica para colocar órdenes basada en las señales
    # Por ejemplo, si no hay posiciones abiertas y hay señal de compra, etc.
    if buy_signal and not sell_signal:
        print("\nAcción sugerida: Considerar COMPRA.")
        # Lógica para poner orden de compra
    elif sell_signal and not buy_signal:
        print("\nAcción sugerida: Considerar VENTA.")
        # Lógica para poner orden de venta
    elif buy_signal and sell_signal:
        print("\nConflicto: Señal de compra y venta simultáneas. Revisar o definir prioridad.")
    else:
        print("\nAcción sugerida: Mantener / Sin señal clara.")

if __name__ == "__main__":
    run_strategy()