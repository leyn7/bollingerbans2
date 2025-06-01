import os
from binance.client import Client # Para constantes como KLINE_INTERVAL_5MINUTE
from decimal import Decimal # Asegúrate que Decimal esté importado
from dotenv import load_dotenv

load_dotenv()
# --- Binance API Keys (Cargadas desde Variables de Entorno) ---
API_KEY = os.environ.get('BINANCE_API_KEY_FUTURES_REAL')
API_SECRET = os.environ.get('BINANCE_API_SECRET_FUTURES_REAL')

# --- Configuración del Símbolo de Trading e Intervalos ---
SYMBOL_CFG = "BNBUSDT" # Símbolo por defecto si no se usa symbols_config.json
INTERVAL_5M_CFG = Client.KLINE_INTERVAL_5MINUTE
INTERVAL_1M_CFG = Client.KLINE_INTERVAL_1MINUTE # Usado por BB_buy/sell para visualización interna
INTERVAL_15M_CFG = Client.KLINE_INTERVAL_15MINUTE # Usado para el SL de referencia (BBM)
# Mantener estas si son usadas explícitamente en alguna parte, aunque redundantes con la de arriba
INTERVAL_15M_CLIENT_CONST = Client.KLINE_INTERVAL_15MINUTE
INTERVAL_15M_STR_CFG = "15m"

ENABLE_CTX_SNAPSHOT_LOG = True # True para activar, False para desactivar
PRICE_PRECISION_LOG_CTX = 5    # Precisión para los precios en el log contextual

# --- Parámetros de la Estrategia de Bandas de Bollinger ---
MA_TYPE_CFG = "SMA"
LENGTH_CFG = 20
MULT_ORIG_CFG = 2.0
MULT_NEW_CFG = 1.0
DATA_LIMIT_5M_CFG = 300
RISK_REWARD_MULTIPLIER = 10.0 # RRR 1:10 como mencionaste

# --- Configuración de Trailing Stop Loss (TSL) ---
ENABLE_TRAILING_STOP_LOSS_ORIG_BANDS = False
SEND_TELEGRAM_TSL_UPDATES = False

# --- Gestión de Riesgo ---
# Modo de Riesgo Principal
USE_PERCENTAGE_RISK_MANAGEMENT = False  # True para arriesgar un % del capital, False para usar fixed_quantity como base para cantidad
RISK_PERCENTAGE_PER_TRADE = 0.2       # Porcentaje del capital a arriesgar si USE_PERCENTAGE_RISK_MANAGEMENT = True (ej. 0.2 para 0.2%)

# Opción para Stop Loss Basado en Riesgo Monetario Fijo (INDEPENDIENTE del % de riesgo para cantidad)
USE_FIXED_MONETARY_RISK_SL = True      # ✅ NUEVO: True para activar SL basado en un monto fijo (ej. $1)
                                       # Si es True, el SL se calculará para no perder más de FIXED_MONETARY_RISK_PER_TRADE.
                                       # La cantidad de la operación idealmente debería ser fija (de symbol_params) en este modo.
FIXED_MONETARY_RISK_PER_TRADE = Decimal('0.5') # ✅ NUEVO: Monto fijo a arriesgar por operación si USE_FIXED_MONETARY_RISK_SL = True (ej. 1 USDT)

# Opción para Recuperación de Pérdidas (Martingala Modificada)
USE_MARTINGALE_LOSS_RECOVERY = True    # ✅ NUEVO: True para activar la lógica de recuperación de pérdidas.
                                       # Aumenta el RIESGO MONETARIO del siguiente trade para cubrir pérdidas anteriores.
                                       # Requiere que RISK_REWARD_MULTIPLIER sea > 0.

# --- Parámetros de Ejecución de Trading ---
# FIXED_TRADE_QUANTITY se define ahora por símbolo en symbols_config.json
# LEVERAGE_FUTURES_CFG se define ahora por símbolo en symbols_config.json
#DEFAULT_FIXED_TRADE_QUANTITY = 0.1 # Fallback si no está en symbols_config.json (Comentado porque se prioriza symbols_config.json)
DEFAULT_LEVERAGE_FUTURES_CFG = 5    # Fallback si no está en symbols_config.json

# --- Configuración de Operación del Bot ---
LOOP_SLEEP_SECONDS = 15 # Ciclo principal del bot
STATE_FILE_PATH = "bot_trading_state.json"
VERBOSE_SIGNAL_ANALYSIS = False # Para BB_buy/BB_sell

# --- Configuración de Filtros Adicionales ---
BBW_SQUEEZE_FILTER_ENABLED = False     # True para activar el filtro, False para desactivarlo
BBW_SQUEEZE_THRESHOLD = 0.06       # Umbral para el BBW Squeeze.

# --- Configuración de Logging ---
LOG_LEVEL = "DEBUG" # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FILE = "trading_bot_activity.log"

# --- Configuración de Símbolos Dinámicos ---
SYMBOLS_CONFIG_FILE_PATH = "symbols_config.json" # Nombre del archivo JSON para los símbolos

# --- Configuración de Telegram ---
TELEGRAM_ENABLED = True  # True para activar notificaciones/comandos, False para desactivar
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN_ENV')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID_ENV')

# --- Plantillas de Mensajes de Telegram ---
TELEGRAM_MSG_SL_FILLED = (
    "<b>🛑 CIERRE POR STOP LOSS 🛑</b>\n\n"
    "Símbolo: <code>{symbol}</code>\n"
    "Dirección: <code>{direction}</code>\n"
    "Cantidad: <code>{quantity}</code>\n\n"
    "Precio Entrada: <code>{entry_price}</code>\n"
    "Precio Cierre SL: <code>{close_price}</code>\n\n"
    "📉 <b>Pérdida del Trade: {pnl} {asset_pnl}</b>\n"
    "💰 Balance de Cuenta Actual: <code>{balance} {asset_balance}</code>"
)

TELEGRAM_MSG_TP_FILLED = (
    "<b>✅ CIERRE POR TAKE PROFIT ✅</b>\n\n"
    "Símbolo: <code>{symbol}</code>\n"
    "Dirección: <code>{direction}</code>\n"
    "Cantidad: <code>{quantity}</code>\n\n"
    "Precio Entrada: <code>{entry_price}</code>\n"
    "Precio Cierre TP: <code>{close_price}</code>\n\n"
    "📈 <b>Ganancia del Trade: {pnl} {asset_pnl}</b>\n"
    "💰 Balance de Cuenta Actual: <code>{balance} {asset_balance}</code>"
)

TELEGRAM_MSG_POSITION_NO_SL_ALERT = (
    "<b>⚠️ ALERTA CRÍTICA: POSICIÓN SIN PROTECCIÓN ⚠️</b>\n\n"
    "Símbolo: <code>{symbol}</code>\n"
    "Dirección: <code>{direction}</code>\n"
    "Cantidad: <code>{quantity}</code>\n"
    "Precio Entrada: <code>{entry_price}</code>\n\n"
    "<i>Por favor, revisa y establece un Stop Loss manualmente si es necesario.</i>"
)

TELEGRAM_MSG_SL_UPDATED = (
    "<b>⚙️ STOP LOSS ACTUALIZADO (TSL) ⚙️</b>\n\n"
    "Símbolo: <code>{symbol}</code>\n"
    "Dirección: <code>{direction}</code>\n"
    "Precio Entrada: <code>{entry_price}</code>\n"
    "SL Anterior: <code>{old_sl_price}</code>\n"
    "SL Nuevo: <code>{new_sl_price}</code>\n\n"
    "<i>Posición sigue activa.</i>"
)

TELEGRAM_MSG_IMPORTANT_UPDATE = (
    "📢 <b>NOVEDAD IMPORTANTE DEL BOT</b> 📢\n\n"
    "{message}" # Si 'message' contiene tags HTML, se renderizarán.
)

# --- Configuración de Zona Horaria ---
LOCAL_TIMEZONE_STR_CFG = 'America/Bogota'


# --- Validaciones Básicas de Configuración ---
if not API_KEY or not API_SECRET:
    print("Error Crítico de Configuración: Las variables de entorno para API_KEY y API_SECRET no están configuradas.")
    # exit() # Considera si salir o solo advertir

if TELEGRAM_ENABLED:
    if not TELEGRAM_BOT_TOKEN:
        print("ADVERTENCIA de Configuración: TELEGRAM_ENABLED es True, pero la variable de entorno 'TELEGRAM_BOT_TOKEN_ENV' no está configurada.")
    if not TELEGRAM_CHAT_ID:
        print("ADVERTENCIA de Configuración: TELEGRAM_ENABLED es True, pero la variable de entorno 'TELEGRAM_CHAT_ID_ENV' no está configurada.")

# Nueva validación para las estrategias de riesgo
if USE_FIXED_MONETARY_RISK_SL and USE_PERCENTAGE_RISK_MANAGEMENT:
    print("ADVERTENCIA de Configuración: USE_FIXED_MONETARY_RISK_SL y USE_PERCENTAGE_RISK_MANAGEMENT están ambos en True. "
          "Si USE_FIXED_MONETARY_RISK_SL es True, el SL se calculará para un monto fijo, "
          "y la cantidad de la operación idealmente debería ser fija (de symbol_params) en lugar de basarse en % de riesgo del capital "
          "que a su vez depende de un SL de referencia.")

# RISK_REWARD_MULTIPLIER también está definido arriba.
if USE_MARTINGALE_LOSS_RECOVERY and (RISK_REWARD_MULTIPLIER is None or RISK_REWARD_MULTIPLIER <= 0):
    print("ERROR de Configuración: USE_MARTINGALE_LOSS_RECOVERY es True, pero RISK_REWARD_MULTIPLIER no está definido correctamente o es <= 0. Martingala no funcionará.")
    # exit()

# print("Configuración cargada.")