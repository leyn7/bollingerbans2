# Archivo: telegram_manager.py
import logging
import json
import sys
import time
from telegram import Update, Bot, LinkPreviewOptions # Importado LinkPreviewOptions
import telegram # Importado telegram para telegram.constants.ParseMode.HTML
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults
from telegram.error import NetworkError, RetryAfter, TimedOut
import os
import asyncio
import threading
from binance.client import Client # Asegúrate de tener Client importado
LOCAL_CLIENT_INTERVAL_TO_STRING_MAP_TG = {
    Client.KLINE_INTERVAL_1MINUTE: "1m",
    Client.KLINE_INTERVAL_5MINUTE: "5m",
    Client.KLINE_INTERVAL_15MINUTE: "15m",
    # Añade otros si los necesitas o si tus configs de intervalo usan más constantes
}

import config # Asumiendo que config.py está en el mismo directorio o accesible
# from trade_manager import TradeManager # Descomentar si es necesario para alguna funcionalidad específica (ej. _close_positions_command)
# from persistent_state import PersistentState # Descomentar si es necesario

logger = logging.getLogger(__name__)
SYMBOLS_CONFIG_FILE_PATH = getattr(config, 'SYMBOLS_CONFIG_FILE_PATH', 'symbols_config.json')

class TelegramManager:
    def __init__(self, token: str, admin_chat_id: str,
                 binance_client_instance=None, # Para TradeManager en _close_positions_command
                 persistent_state_instance=None): # Para limpiar estado en _close_positions_command
        self.token = token
        self.admin_chat_id = str(admin_chat_id) # Asegurar que es string
        self.bot_operational = True # Estado global del bot (para habilitar/deshabilitar operaciones)
        self.active_trading_symbols_config = {} # Diccionario para la configuración de símbolos
        self.binance_client = binance_client_instance # Instancia del cliente de Binance
        self.bot_state = persistent_state_instance # Instancia de PersistentState

        self.application = None
        self.bot = None # Se inicializará en _build_and_register_handlers
        self.telegram_loop = None
        self.telegram_thread_id = None
        self.shutdown_event = None # asyncio.Event para el apagado ordenado

        self._load_symbols_config()
        logger.info("TelegramManager pre-inicializado (la aplicación se construirá en el hilo).")

    def _build_and_register_handlers(self):
        try:
            # Intenta usar LinkPreviewOptions si está disponible (python-telegram-bot >= v20.0)
            link_preview_options = LinkPreviewOptions(is_disabled=True)
            defaults = Defaults(parse_mode=telegram.constants.ParseMode.HTML, link_preview_options=link_preview_options)
        except AttributeError:
            # Fallback para versiones anteriores (python-telegram-bot < v20.0)
            defaults = Defaults(parse_mode=telegram.constants.ParseMode.HTML, disable_web_page_preview=True) # type: ignore

        self.application = Application.builder().token(self.token).defaults(defaults).build()
        self.bot = self.application.bot # Asignar self.bot después de construir la aplicación

        handlers = [
            CommandHandler("start", self._start_command),
            CommandHandler("status", self._status_command),
            CommandHandler("help", self._help_command),
            CommandHandler("bot_on", self._bot_on_command),
            CommandHandler("bot_off", self._bot_off_command),
            CommandHandler("add", self._add_symbol_command),
            CommandHandler("remove", self._remove_symbol_command),
            CommandHandler("list", self._list_symbols_command),
            CommandHandler("enable_sym", self._enable_symbol_command),
            CommandHandler("disable_sym", self._disable_symbol_command),
            CommandHandler("get_json", self._get_config_json_command),
            CommandHandler("close_pos", self._close_positions_command)
            # Añade más handlers aquí si es necesario
        ]
        for handler in handlers:
            self.application.add_handler(handler)
        logger.info("Bot TG (hilo): Aplicación construida y handlers registrados.")

    def _is_admin(self, update: Update) -> bool:
        return str(update.effective_chat.id) == self.admin_chat_id

    async def _send_message_to_admin_internal(self, text: str):
        logger.debug(f"TG_MGR (_send_internal): Intentando enviar a admin_chat_id: '{self.admin_chat_id}'. Mensaje: '{text[:70]}...'")
        if not self.admin_chat_id or not self.bot:
            logger.error(f"TG_MGR (_send_internal): admin_chat_id o self.bot no configurados. No se puede enviar mensaje.")
            return
        try:
            await self.bot.send_message(chat_id=self.admin_chat_id, text=text, parse_mode=telegram.constants.ParseMode.HTML)
            logger.debug(f"TG_MGR (_send_internal): Mensaje TG enviado al admin_chat_id. Texto: '{text[:70]}...'")
        except Exception as e:
            logger.error(f"TG_MGR (_send_internal): Error enviando mensaje TG al admin_chat_id: {e}. Texto: '{text[:70]}...'", exc_info=True)

    def send_message_to_admin_threadsafe(self, text: str):
        current_thread_ident = threading.get_ident()
        logger.debug(f"TG_MGR (send_threadsafe): Solicitud para enviar mensaje desde hilo {current_thread_ident}. Hilo Telegram: {self.telegram_thread_id}. Loop Telegram: {'Activo' if self.telegram_loop and self.telegram_loop.is_running() else 'Inactivo o no asignado'}.")

        if self.telegram_loop and self.telegram_loop.is_running():
            if current_thread_ident == self.telegram_thread_id:
                # Si estamos en el mismo hilo del loop de Telegram, podemos crear la tarea directamente.
                logger.debug(f"TG_MGR (send_threadsafe): Ejecutando _send_message_to_admin_internal como tarea en el loop actual (hilo {current_thread_ident}).")
                self.telegram_loop.create_task(self._send_message_to_admin_internal(text))
            else:
                # Si estamos en un hilo diferente, usamos run_coroutine_threadsafe.
                logger.debug(f"TG_MGR (send_threadsafe): Usando run_coroutine_threadsafe para enviar desde hilo {current_thread_ident} al loop del hilo {self.telegram_thread_id}.")
                future = asyncio.run_coroutine_threadsafe(self._send_message_to_admin_internal(text), self.telegram_loop)
                try:
                    future.result(timeout=15) # Esperar a que la corutina termine, con un timeout.
                    logger.debug(f"TG_MGR (send_threadsafe): future.result() completado para mensaje: '{text[:70]}...'.")
                except asyncio.TimeoutError:
                    logger.error(f"TG_MGR (send_threadsafe): Timeout (15s) esperando future.result() para mensaje: '{text[:70]}...'. El mensaje podría haberse enviado igualmente.")
                except Exception as e:
                    logger.error(f"TG_MGR (send_threadsafe): Excepción en future.result(): {e} para mensaje: '{text[:70]}...'", exc_info=True)
        else:
            logger.warning(f"TG_MGR (send_threadsafe): Bucle asyncio de Telegram no disponible o no está corriendo. No se envió mensaje: '{text[:70]}...'")

    def _load_symbols_config(self) -> None:
        if os.path.exists(SYMBOLS_CONFIG_FILE_PATH):
            try:
                with open(SYMBOLS_CONFIG_FILE_PATH, 'r', encoding='utf-8') as f:
                    self.active_trading_symbols_config = json.load(f)
                logger.info(f"Configuración de símbolos cargada desde {SYMBOLS_CONFIG_FILE_PATH}")
                return
            except Exception as e:
                logger.error(f"Error cargando {SYMBOLS_CONFIG_FILE_PATH}: {e}. Se usará/creará configuración por defecto.")
        
        logger.info(f"Archivo {SYMBOLS_CONFIG_FILE_PATH} no encontrado o con error. Creando/usando configuración de símbolos por defecto.")
        default_symbol = getattr(config, 'SYMBOL_CFG', "BNBUSDT") # Usar SYMBOL_CFG de config.py
        
        # Mapeo para convertir constantes de cliente a strings si es necesario
        # Asumimos que config.py tiene CLIENT_INTERVAL_TO_STRING_MAP o las constantes directamente
        interval_map_for_config = getattr(config, 'LOCAL_CLIENT_INTERVAL_TO_STRING_MAP', 
                                          {config.INTERVAL_5M_CFG: "5m", config.INTERVAL_1M_CFG: "1m"} if hasattr(config, 'INTERVAL_5M_CFG') and hasattr(config, 'INTERVAL_1M_CFG') else {"5m": "5m", "1m": "1m"})

        self.active_trading_symbols_config = {
            default_symbol: {
                "interval_5m": interval_map_for_config.get(getattr(config, 'INTERVAL_5M_CFG', '5m'), "5m"),
                "interval_1m": interval_map_for_config.get(getattr(config, 'INTERVAL_1M_CFG', '1m'), "1m"),
                "ma_type": getattr(config, 'MA_TYPE_CFG', "SMA"),
                "length": getattr(config, 'LENGTH_CFG', 20),
                "mult_orig": getattr(config, 'MULT_ORIG_CFG', 2.0),
                "mult_new": getattr(config, 'MULT_NEW_CFG', 1.0),
                "data_limit_5m": getattr(config, 'DATA_LIMIT_5M_CFG', 300),
                "fixed_quantity": getattr(config, 'DEFAULT_FIXED_TRADE_QUANTITY', 0.1), # Usar el fallback de config.py si existe
                "leverage": getattr(config, 'DEFAULT_LEVERAGE_FUTURES_CFG', 5), # Usar el fallback de config.py
                "active": True
            }
        }
        self._save_symbols_config()

    def _save_symbols_config(self) -> None:
        try:
            with open(SYMBOLS_CONFIG_FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.active_trading_symbols_config, f, indent=4)
            logger.info(f"Configuración de símbolos guardada en {SYMBOLS_CONFIG_FILE_PATH}")
        except Exception as e:
            logger.error(f"Error guardando configuración de símbolos en {SYMBOLS_CONFIG_FILE_PATH}: {e}", exc_info=True)

    def get_symbols_to_trade(self) -> dict:
        return {s: p for s, p in self.active_trading_symbols_config.items() if p.get("active", False)}

    def is_bot_globally_enabled(self) -> bool:
        return self.bot_operational

    # --- Command Handlers ---
    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        await update.message.reply_text("Bot de Trading BB - Telegram Manager listo. Usa /help para ver los comandos.")

    async def _help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        help_text = (
            "<b>Comandos Disponibles:</b>\n"
            "/status - Muestra el estado global del bot y de los símbolos.\n"
            "/bot_on - Habilita globalmente el bot para operar.\n"
            "/bot_off - Deshabilita globalmente el bot (no abrirá nuevos trades).\n"
            "/add SYMBOL TF (ej: 5m) [qty] [lev] - Añade un símbolo (TF: 1m,5m,15m). Qty y Lev opcionales.\n"
            "/remove SYMBOL - Elimina un símbolo de la configuración.\n"
            "/list - Lista los símbolos configurados y su estado.\n"
            "/enable_sym SYMBOL - Activa el trading para un símbolo específico.\n"
            "/disable_sym SYMBOL - Desactiva el trading para un símbolo específico.\n"
            "/get_json - Envía el archivo JSON de configuración de símbolos.\n"
            "/close_pos SYMBOL - Intenta cerrar todas las posiciones para el símbolo especificado.\n"
            "/help - Muestra este mensaje de ayuda."
        )
        await update.message.reply_text(help_text)

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        bot_global_status_str = "✅ HABILITADO" if self.bot_operational else "❌ DESHABILITADO"
        # CORRECCIÓN: Inicializar 'text' aquí
        text = f"<b><u>Estado Global del Bot:</u></b> {bot_global_status_str}\n\n<b>Símbolos Configurados:</b>\n"

        if not self.active_trading_symbols_config:
            text += " <i>Ninguno configurado. Usa /add para añadir.</i>"
        else:
            for symbol_key, params in self.active_trading_symbols_config.items():
                status_sym = "✅ Activo" if params.get("active", False) else "❌ Inactivo"
                tf_sym = params.get('interval_5m', 'N/A')
                qty_sym = params.get('fixed_quantity', 'N/A')
                lev_sym = params.get('leverage', 'N/A')
                text += f" - <b>{symbol_key}</b>: {status_sym} (TF: {tf_sym}, Qty: {qty_sym}, Lev: {lev_sym})\n"
        
        await update.message.reply_text(text)

    async def _bot_on_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        self.bot_operational = True
        await update.message.reply_text("✅ Bot HABILITADO globalmente. El bot ahora puede iniciar nuevas operaciones.")
        logger.info("Bot HABILITADO globalmente vía Telegram.")

    async def _bot_off_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        self.bot_operational = False
        await update.message.reply_text("❌ Bot DESHABILITADO globalmente. No se iniciarán nuevas operaciones.")
        logger.info("Bot DESHABILITADO globalmente vía Telegram.")

    async def _add_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        args = context.args

        # Comando esperado: /add SYMBOL TF_Principal [fixed_quantity_opcional] [leverage_opcional]
        # Ejemplo: /add DOGEUSDT 5m 100 10
        if not args or not (2 <= len(args) <= 4):
            await update.message.reply_text(
                "Uso: /add SYMBOL TF_Principal (ej: 5m) [cantidad_fija opc] [apalancamiento opc]\n"
                "Ej: /add DOGEUSDT 5m 100 10  -> Añade DOGEUSDT en 5m, cantidad 100, apalancamiento 10x\n"
                "Ej: /add XRPUSDT 15m -> Añade XRPUSDT en 15m, usa cantidad y apalancamiento por defecto de config.py"
            )
            return

        symbol = args[0].upper()
        tf_principal_str = args[1].lower() # Este será tu "interval_5m"

        # Validar el timeframe principal
        if tf_principal_str not in ["1m", "5m", "15m"]: # Ajusta si permites otros TFs principales
            await update.message.reply_text("Timeframe principal inválido. Usar: 1m, 5m, o 15m.")
            return

        if symbol in self.active_trading_symbols_config:
            await update.message.reply_text(f"El símbolo {symbol} ya existe en la configuración.")
            return

        # Obtener cantidad y apalancamiento: desde los argumentos o desde config.py como fallback
        # Nota: DEFAULT_FIXED_TRADE_QUANTITY estaba comentado en tu config.py. 
        # getattr lo tomará si está definido, si no, usará el default aquí (0.1).
        # Si quieres un default global diferente, descoméntalo y defínelo en config.py.
        fixed_qty_str = args[2] if len(args) > 2 else str(getattr(config, 'DEFAULT_FIXED_TRADE_QUANTITY', 0.1))
        leverage_str = args[3] if len(args) > 3 else str(getattr(config, 'DEFAULT_LEVERAGE_FUTURES_CFG', 5))
        
        try:
            current_fixed_qty = float(fixed_qty_str) # o Decimal(fixed_qty_str) si prefieres, sé consistente
            current_leverage = int(leverage_str)
            if current_fixed_qty <= 0 or current_leverage <= 0:
                await update.message.reply_text("Error: La cantidad y el apalancamiento deben ser positivos.")
                return
        except ValueError:
            await update.message.reply_text("Error: fixed_quantity debe ser un número y leverage un entero.")
            return

        # Construir la entrada para el nuevo símbolo usando los globales de config.py
        # para los parámetros de la estrategia
        self.active_trading_symbols_config[symbol] = {
            "interval_5m": tf_principal_str, # El que el usuario proveyó
            "interval_1m": LOCAL_CLIENT_INTERVAL_TO_STRING_MAP_TG.get(config.INTERVAL_1M_CFG, "1m"), # Default global
            "ma_type": config.MA_TYPE_CFG,
            "length": int(config.LENGTH_CFG),
            "mult_orig": float(config.MULT_ORIG_CFG),
            "mult_new": float(config.MULT_NEW_CFG),
            "data_limit_5m": int(config.DATA_LIMIT_5M_CFG),
            "fixed_quantity": current_fixed_qty, # El que el usuario proveyó o el default de config
            "leverage": current_leverage,      # El que el usuario proveyó o el default de config
            "active": True # Se añade como activo por defecto
        }
        self._save_symbols_config() # Guardar en el archivo JSON
        await update.message.reply_text(
            f"<b>{symbol}</b> añadido:\n"
            f"  TF Principal: {tf_principal_str}\n"
            f"  TF Trigger: {self.active_trading_symbols_config[symbol]['interval_1m']}\n"
            f"  Estrategia: {config.MA_TYPE_CFG}{config.LENGTH_CFG}, x{config.MULT_ORIG_CFG}, x{config.MULT_NEW_CFG}\n"
            f"  Cantidad: {current_fixed_qty}\n"
            f"  Apalancamiento: {current_leverage}x\n"
            f"  Estado: Activo"
        )
        logger.info(f"Símbolo {symbol} (TF: {tf_principal_str}, Qty: {current_fixed_qty}, Lev: {current_leverage}x) añadido vía Telegram con parámetros de config.py.")
    async def _remove_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        # CORRECCIÓN: Inicializar 'args' aquí
        args = context.args
        
        if not args or len(args) != 1:
            await update.message.reply_text("Uso: /remove SYMBOL")
            return
        
        symbol_to_remove = args[0].upper()
        if symbol_to_remove in self.active_trading_symbols_config:
            del self.active_trading_symbols_config[symbol_to_remove]
            self._save_symbols_config()
            await update.message.reply_text(f"<b>{symbol_to_remove}</b> eliminado de la configuración.")
            logger.info(f"Símbolo {symbol_to_remove} eliminado vía Telegram.")
        else:
            await update.message.reply_text(f"Símbolo {symbol_to_remove} no encontrado en la configuración.")

    async def _list_symbols_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        if not self.active_trading_symbols_config:
            await update.message.reply_text("No hay símbolos configurados. Usa /add para añadir.")
            return
        
        message_text = "<b>Símbolos Configurados:</b>\n"
        for symbol_key, params in self.active_trading_symbols_config.items():
            status_sym = "✅ Activo" if params.get("active", False) else "❌ Inactivo"
            tf_sym = params.get('interval_5m', 'N/A')
            qty_sym = params.get('fixed_quantity', 'N/A')
            lev_sym = params.get('leverage', 'N/A')
            message_text += f" - <b>{symbol_key}</b>: {status_sym} (TF: {tf_sym}, Qty: {qty_sym}, Lev: {lev_sym})\n"
        
        await update.message.reply_text(message_text)

    async def _enable_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        # CORRECCIÓN: Inicializar 'args' aquí
        args = context.args

        if not args or len(args) != 1:
            await update.message.reply_text("Uso: /enable_sym SYMBOL")
            return
            
        symbol_to_enable = args[0].upper()
        if symbol_to_enable in self.active_trading_symbols_config:
            self.active_trading_symbols_config[symbol_to_enable]["active"] = True
            self._save_symbols_config()
            await update.message.reply_text(f"<b>{symbol_to_enable}</b> ✅ HABILITADO para trading.")
            logger.info(f"Símbolo {symbol_to_enable} HABILITADO vía Telegram.")
        else:
            await update.message.reply_text(f"Símbolo {symbol_to_enable} no encontrado en la configuración.")

    async def _disable_symbol_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        
        # CORRECCIÓN: Inicializar 'args' aquí
        args = context.args
        
        if not args or len(args) != 1:
            await update.message.reply_text("Uso: /disable_sym SYMBOL")
            return
            
        symbol_to_disable = args[0].upper()
        if symbol_to_disable in self.active_trading_symbols_config:
            self.active_trading_symbols_config[symbol_to_disable]["active"] = False
            self._save_symbols_config()
            await update.message.reply_text(f"<b>{symbol_to_disable}</b> ❌ DESHABILITADO para trading.")
            logger.info(f"Símbolo {symbol_to_disable} DESHABILITADO vía Telegram.")
        else:
            await update.message.reply_text(f"Símbolo {symbol_to_disable} no encontrado en la configuración.")

    async def _get_config_json_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not os.path.exists(SYMBOLS_CONFIG_FILE_PATH):
            await update.message.reply_text(f"Archivo de configuración de símbolos ({SYMBOLS_CONFIG_FILE_PATH}) no encontrado.")
            return
        try:
            await update.message.reply_document(document=open(SYMBOLS_CONFIG_FILE_PATH, 'rb'), filename=os.path.basename(SYMBOLS_CONFIG_FILE_PATH))
            logger.info(f"Archivo {SYMBOLS_CONFIG_FILE_PATH} enviado al admin vía Telegram.")
        except Exception as e:
            logger.error(f"Error enviando archivo {SYMBOLS_CONFIG_FILE_PATH} vía Telegram: {e}", exc_info=True)
            await update.message.reply_text(f"Error al intentar enviar el archivo: {e}")

    async def _close_positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        log_ctx_tg = "TelegramCmd (/close_pos)"
        user_who_commanded = update.effective_user.username or f"ID:{update.effective_chat.id}"
        if not self._is_admin(update):
            logger.warning(f"{log_ctx_tg}: Comando recibido de usuario no administrador: {user_who_commanded}")
            return
        
        # CORRECCIÓN: Inicializar 'args' aquí
        args = context.args
        
        if not args or len(args) != 1:
            await update.message.reply_text("Uso: /close_pos SYMBOL\n(Ejemplo: /close_pos BTCUSDT para cerrar posiciones de BTCUSDT)")
            return
        
        symbol_to_close = args[0].upper()
        logger.info(f"{log_ctx_tg}: Solicitud de cierre para todas las posiciones de {symbol_to_close} recibida de {user_who_commanded}.")
        await update.message.reply_text(f"Recibida solicitud para cerrar todas las posiciones de {symbol_to_close}. Intentando...")

        if not self.binance_client or not self.bot_state:
            err_msg = "Error interno crítico: Binance client o bot_state no están disponibles para TelegramManager."
            logger.error(f"{log_ctx_tg}: {err_msg}")
            await update.message.reply_text(err_msg)
            return

        try:
            # Necesitas una instancia de TradeManager para esto. Asumimos que puedes crear una temporalmente
            # o que tienes una forma de acceder a una instancia relevante.
            # Para esta función, crear una temporal es más seguro si no hay una global.
            # Esto requiere que TradeManager se importe: from trade_manager import TradeManager
            from trade_manager import TradeManager # Importación local si no está global
            temp_trade_manager = TradeManager(self.binance_client, symbol_to_close) # Usa el símbolo específico
            
            success_closing = temp_trade_manager.close_all_symbol_positions(symbol_target=symbol_to_close) # Pasar symbol_target
            
            if success_closing:
                msg_reply = f"✅ Cierre de todas las posiciones en el exchange para {symbol_to_close} parece haber finalizado. Revisa el exchange para confirmar."
                logger.info(f"{log_ctx_tg}: {msg_reply}")
                await update.message.reply_text(msg_reply)
                
                # Limpiar estado del bot para este símbolo
                reason_clear = f"Cerrado vía comando /close_pos por {user_who_commanded}"
                for side_suffix in ["_LONG", "_SHORT"]:
                    state_key = f"{symbol_to_close}{side_suffix}"
                    if self.bot_state.get_active_trade(state_key):
                        self.bot_state.clear_active_trade(state_key, reason_clear)
                        await update.message.reply_text(f"Estado interno del bot para {state_key} limpiado.")
                await update.message.reply_text(f"Operación /close_pos para {symbol_to_close} completada desde el bot.")
            else:
                msg_reply = (f"⚠️ Se encontraron problemas o no había posiciones activas para cerrar en el exchange para {symbol_to_close}. "
                             f"Revisa los logs del bot y el exchange.")
                logger.warning(f"{log_ctx_tg}: {msg_reply}") # Cambiado a warning, ya que puede que no hubiera posiciones
                await update.message.reply_text(msg_reply)

        except ImportError:
            logger.error(f"{log_ctx_tg}: No se pudo importar TradeManager para el comando /close_pos.")
            await update.message.reply_text("Error interno: Falta el módulo TradeManager para ejecutar este comando.")
        except Exception as e:
            logger.error(f"{log_ctx_tg}: Excepción durante el comando /close_pos para {symbol_to_close}: {e}", exc_info=True)
            await update.message.reply_text(f"Error inesperado al procesar el cierre para {symbol_to_close}. Revisa los logs.")

    # --- Manejador de Errores del Polling ---
    async def _polling_error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Maneja errores que ocurren durante el polling de get_updates."""
        error = context.error
        logger.error(f"Bot TG (hilo): Error durante el polling (get_updates): {error}", exc_info=error) # Loguear el error completo con traceback
        
        if isinstance(error, NetworkError):
            logger.warning(f"Bot TG (hilo): Error de red en polling: {error}. El updater reintentará automáticamente.")
        elif isinstance(error, RetryAfter):
            logger.warning(f"Bot TG (hilo): Flood control. Telegram solicitó reintentar después de {error.retry_after} segundos.")
            await asyncio.sleep(error.retry_after) # Esperar antes de que el polling continúe
        elif isinstance(error, TimedOut):
            logger.warning(f"Bot TG (hilo): Timeout durante el polling. El updater reintentará.")
        # Considerar otros errores específicos de telegram.error si es necesario

    # --- Corutina Principal y Arranque del Hilo ---
    async def _telegram_main_coroutine(self):
        polling_task = None
        try:
            if not self.application: # Si no se construyó en __init__ (ej. por tests)
                self._build_and_register_handlers()
            
            if not self.application: # Chequeo final
                logger.error("Bot TG (hilo): Fallo crítico al construir la aplicación de Telegram. La corutina no puede continuar.")
                return

            # Añadir el manejador de errores de polling
            self.application.add_error_handler(self._polling_error_handler)

            logger.info("Bot TG (hilo): Inicializando la aplicación de Telegram...")
            await self.application.initialize() # Importante para preparar el bot, handlers, etc.
            logger.info("Bot TG (hilo): Aplicación de Telegram inicializada.")

            # Enviar mensaje de "conectado"
            await self._send_message_to_admin_internal("✅ Bot de Telegram conectado y escuchando. Usa /help para ver comandos.")
            
            logger.info("Bot TG (hilo): Iniciando el dispatcher de la aplicación y el polling...")
            await self.application.start() # Inicia el dispatcher

            if self.application.updater:
                # Parámetros de start_polling:
                # allowed_updates: Lista de tipos de updates que el bot debe recibir.
                # timeout: Timeout en segundos para el long polling.
                # read_timeout, connect_timeout: Timeouts para la conexión HTTP (pueden causar problemas si son muy bajos)
                #                                 La librería suele manejarlos bien por defecto.
                #                                 Para la v20+, estos timeouts están en HTTPXSettings.
                polling_task = self.telegram_loop.create_task(
                    self.application.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES, 
                        timeout=30 # Timeout para la petición de long polling
                        # No especificar read_timeout/connect_timeout aquí a menos que se sepa que es necesario
                        # y se haga a través de la configuración de persistencia o httpx_settings si es PTB v20+
                    )
                )
                logger.info("Bot TG (hilo): Polling del updater iniciado como una tarea asyncio.")
            else:
                logger.error("Bot TG (hilo): self.application.updater no está disponible. El polling no se puede iniciar.")
                if self.shutdown_event:
                    self.shutdown_event.set() # Señal para que el hilo termine si el polling no puede empezar
                return

            if self.shutdown_event:
                await self.shutdown_event.wait() # Mantener la corutina viva hasta que se señale el apagado
                logger.info("Bot TG (hilo): Señal de apagado (shutdown_event) recibida en la corutina principal.")
            else:
                # Esto no debería pasar si __init__ o start_telegram_listener_thread lo crean
                logger.error("Bot TG (hilo): shutdown_event no fue creado. El hilo podría no terminar correctamente.")
                return # Salir si no hay forma de coordinar el apagado

        except asyncio.CancelledError:
            logger.info("Bot TG (hilo): Corutina principal de Telegram (_telegram_main_coroutine) fue cancelada.")
            # Esto es esperado durante el apagado si la tarea es cancelada externamente
        except Exception as e:
            logger.critical(f"Bot TG (hilo): Excepción no manejada en la corutina principal de Telegram: {e}", exc_info=True)
            if self.shutdown_event and not self.shutdown_event.is_set(): # Asegurar que se apague
                 self.telegram_loop.call_soon_threadsafe(self.shutdown_event.set)
        finally:
            logger.info("Bot TG (hilo): Entrando al bloque 'finally' de _telegram_main_coroutine. Iniciando limpieza...")
            if self.application:
                try:
                    # 1. Cancelar la tarea de polling explícitamente (si existe y no está hecha)
                    if polling_task and not polling_task.done():
                        logger.info("Bot TG (hilo): Cancelando la tarea de polling...")
                        polling_task.cancel()
                        try:
                            await polling_task # Esperar a que la cancelación se complete
                        except asyncio.CancelledError:
                            logger.info("Bot TG (hilo): Tarea de polling cancelada exitosamente.")
                        except Exception as e_pt_cancel: # Capturar otras posibles excepciones al esperar la tarea
                            logger.error(f"Bot TG (hilo): Excepción mientras se esperaba la cancelación de polling_task: {e_pt_cancel}")
                    
                    # 2. Detener el updater (si existe y está corriendo)
                    if self.application.updater and self.application.updater.running:
                        logger.info("Bot TG (hilo): Deteniendo el updater (llamada síncrona a updater.stop())...")
                        self.application.updater.stop() # Esto es síncrono
                        logger.info("Bot TG (hilo): Updater.stop() completado.")
                    
                    # 3. Detener el dispatcher de la aplicación (si está corriendo)
                    if self.application.running: # Chequear si el Application (dispatcher) está corriendo
                        logger.info("Bot TG (hilo): Deteniendo la aplicación/dispatcher (llamada asíncrona a application.stop())...")
                        await self.application.stop()
                        logger.info("Bot TG (hilo): Application.stop() completado.")
                    
                    # 4. Shutdown final de la aplicación
                    logger.info("Bot TG (hilo): Realizando el shutdown final de la aplicación (llamada asíncrona a application.shutdown())...")
                    await self.application.shutdown()
                    logger.info("Bot TG (hilo): Application.shutdown() completado.")

                except RuntimeError as re: # Común si el loop ya está cerrado o en proceso de cierre
                    logger.error(f"Bot TG (hilo): RuntimeError durante la limpieza de la aplicación de Telegram: {re}")
                except Exception as e_shutdown:
                    logger.error(f"Bot TG (hilo): Excepción general durante la limpieza de la aplicación de Telegram: {e_shutdown}", exc_info=True)
            logger.info("Bot TG (hilo): Limpieza de la aplicación de Telegram en _telegram_main_coroutine (finally) completada.")


    def start_telegram_listener_thread(self):
        """Inicia el bucle de eventos de asyncio para Telegram en un hilo separado."""
        self.telegram_thread_id = threading.get_ident() # Obtener ID del hilo actual (que será el de Telegram)
        
        try:
            # Intentar crear un nuevo bucle de eventos. Si falla (porque ya existe uno en el hilo principal), obtener el existente.
            # Sin embargo, para un hilo separado, siempre es mejor un nuevo bucle.
            self.telegram_loop = asyncio.new_event_loop()
        except RuntimeError: # Raro en un nuevo hilo, pero por si acaso
            logger.warning("TelegramManager: RuntimeError al crear nuevo event loop, intentando obtener el existente.")
            self.telegram_loop = asyncio.get_event_loop()
            
        asyncio.set_event_loop(self.telegram_loop) # Establecer este bucle como el actual para este hilo
        self.shutdown_event = asyncio.Event() # Crear el evento de apagado para este loop

        logger.info(f"Iniciando hilo de Telegram (Thread ID: {self.telegram_thread_id}) con bucle asyncio dedicado (Loop ID: {id(self.telegram_loop)}).")
        try:
            # Ejecutar la corutina principal de Telegram hasta que se complete (o self.shutdown_event la detenga)
            self.telegram_loop.run_until_complete(self._telegram_main_coroutine())
        except Exception as e: # Captura cualquier excepción que pueda terminar run_until_complete prematuramente
            logger.critical(f"Hilo TG: Excepción CRÍTICA no manejada que terminó run_until_complete: {e}", exc_info=True)
            # Asegurar que el evento de apagado se active si no lo está ya, para intentar limpiar
            if self.shutdown_event and not self.shutdown_event.is_set() and self.telegram_loop and not self.telegram_loop.is_closed():
                 self.telegram_loop.call_soon_threadsafe(self.shutdown_event.set)
        finally:
            logger.info("Hilo TG: `run_until_complete` ha finalizado. Iniciando limpieza final del hilo y del bucle asyncio...")
            if self.telegram_loop and not self.telegram_loop.is_closed():
                logger.info("Hilo TG (finally): Cerrando el bucle asyncio de Telegram...")
                try:
                    # Cancelar todas las tareas pendientes en el loop antes de cerrarlo
                    tasks = [t for t in asyncio.all_tasks(loop=self.telegram_loop) if not t.done()]
                    if tasks:
                        logger.info(f"Hilo TG (finally): Cancelando {len(tasks)} tareas asyncio restantes...")
                        for task in tasks:
                            task.cancel()
                        # Esperar a que todas las tareas se cancelen
                        self.telegram_loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                        logger.info("Hilo TG (finally): Todas las tareas restantes han sido procesadas/canceladas.")
                except Exception as e_task_cancel: # Ser defensivo
                       logger.error(f"Hilo TG (finally): Excepción durante la cancelación/espera de tareas pendientes: {e_task_cancel}")
                finally: # Asegurar que el bucle se cierre
                    if not self.telegram_loop.is_closed():
                        self.telegram_loop.close()
                        logger.info("Hilo TG (finally): Bucle asyncio de Telegram cerrado.")
            
            logger.info("Hilo de Telegram: Hilo finalizado completamente.")
            # Resetear para posible reinicio (aunque normalmente el bot terminaría aquí)
            self.telegram_loop = None
            self.telegram_thread_id = None
            self.shutdown_event = None