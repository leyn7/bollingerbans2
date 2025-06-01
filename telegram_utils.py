# Archivo: telegram_utils.py

import telegram # De la librería python-telegram-bot
import logging
import asyncio # Para manejar operaciones asíncronas de la librería

logger = logging.getLogger(__name__)

async def send_telegram_message_async(message_text: str, bot_token: str, chat_id: str) -> bool:
    """
    Envía un mensaje a un chat de Telegram de forma asíncrona.

    Args:
        message_text: El texto del mensaje a enviar.
        bot_token: El token de tu bot de Telegram.
        chat_id: El ID del chat al que se enviará el mensaje.

    Returns:
        True si el mensaje fue enviado exitosamente (o al menos la API lo aceptó), False en caso contrario.
    """
    if not bot_token or not chat_id:
        logger.error("Token del bot de Telegram o Chat ID no configurados. No se puede enviar el mensaje.")
        return False

    try:
        bot = telegram.Bot(token=bot_token)
        # Usamos parse_mode=telegram.constants.ParseMode.MARKDOWN_V2 para soportar el formato que definimos en las plantillas
        # Nota: MarkdownV2 es sensible a caracteres especiales. Si tienes problemas, considera HTML o escapar los caracteres.
        # Para HTML: parse_mode=telegram.constants.ParseMode.HTML
        # Por simplicidad, y dado que tus plantillas usan Markdown básico (negritas), probemos con MarkdownV2.
        # Si los mensajes fallan por formato, podríamos necesitar escapar caracteres como '.', '-', '(', ')', etc.
        # o cambiar a HTML que es más permisivo.

        # Escapado simple para MarkdownV2 (necesitarías una función más robusta para todos los casos)
        # Para este ejemplo, asumiremos que el texto ya está formateado o es simple.
        # Si se usa Markdown en las plantillas (como **texto**), ParseMode.MARKDOWN_V2 o HTML es necesario.
        
        await bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode=telegram.constants.ParseMode.MARKDOWN_V2 # O ParseMode.HTML si prefieres
        )
        logger.debug(f"Mensaje de Telegram enviado a {chat_id}: {message_text[:50]}...") # Loguea solo una parte
        return True
    except telegram.error.TelegramError as e:
        logger.error(f"Error de Telegram al enviar mensaje: {e}")
        # Podrías tener lógica de reintento aquí si es necesario
        return False
    except Exception as e:
        logger.error(f"Error inesperado al enviar mensaje de Telegram: {e}", exc_info=True)
        return False

def send_telegram_message(message_text: str, bot_token: str, chat_id: str) -> bool:
    """
    Wrapper síncrono para enviar un mensaje de Telegram.
    """
    try:
        # Si estás en un entorno que ya tiene un bucle de eventos asyncio (como algunos frameworks web)
        # podrías necesitar obtener el bucle existente. Para un script simple, esto funciona.
        return asyncio.run(send_telegram_message_async(message_text, bot_token, chat_id))
    except RuntimeError as e:
        # Esto puede pasar si asyncio.run() es llamado desde un bucle de eventos ya en ejecución.
        # En ese caso, necesitarías una integración más compleja con el bucle de eventos existente.
        # Para la mayoría de los bots de script, esto debería estar bien.
        logger.error(f"Error de Runtime con asyncio al enviar mensaje de Telegram: {e}. Intenta ejecutar en un hilo separado si estás dentro de un bucle asyncio.")
        # Alternativa si estás en un bucle: loop = asyncio.get_event_loop(); loop.create_task(send_telegram_message_async(...))
        # Pero eso complica el retorno del booleano de éxito.
        # Para un bot que corre linealmente, asyncio.run() es lo más simple.
        return False


if __name__ == '__main__':
    # --- PRUEBA STANDALONE ---
    # Necesitarás configurar estas variables de entorno o poner tus credenciales directamente para probar.
    import os
    from dotenv import load_dotenv
    load_dotenv() # Carga variables de .env si existe

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    TEST_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN_ENV') # Asegúrate que esta variable esté en tu config.py y .env
    TEST_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID_ENV')   # Asegúrate que esta variable esté en tu config.py y .env

    if not TEST_BOT_TOKEN or not TEST_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN_ENV o TELEGRAM_CHAT_ID_ENV no están configurados en el entorno para la prueba.")
    else:
        logger.info("Intentando enviar mensaje de prueba a Telegram...")
        
        # Prueba con MarkdownV2 (asegúrate que los caracteres especiales estén escapados si es necesario)
        # Ejemplo de texto escapado para MarkdownV2 (si fuera necesario):
        # test_message_md = "🛑 *CIERRE POR STOP LOSS* 🛑\n\nSímbolo: BNB-USDT\n*Pérdida*: \-10\.25 USDT" # Note el '\' antes de '-' y '.'
        
        # Usaremos un mensaje simple o uno de tus plantillas (formateado apropiadamente)
        # Para la prueba, un mensaje simple:
        # test_message = "Esta es una *prueba* de notificación del bot de trading vía `telegram_utils.py`."
        # Para usar tus plantillas, tendrías que formatearlas aquí.
        
        # Ejemplo de uso de una de tus plantillas (MODIFICADA PARA MDV2):
        # Los caracteres como '.', '(', ')', '-', '+', '=', '|', '{', '}', '!', '#' deben escaparse con '\'
        # Ejemplo: 'texto.con.puntos' se vuelve 'texto\.con\.puntos'
        
        # Por simplicidad en la prueba, un mensaje formateado con HTML es más robusto si no quieres escapar mucho:
        test_message_html = (
            "<b>🛑 CIERRE POR STOP LOSS 🛑</b>\n\n"
            "Símbolo: <code>BNBUSDT</code>\n"
            "Dirección: <code>LONG</code>\n"
            "Cantidad: <code>0.1</code>\n\n"
            "Precio Entrada: <code>600.1234</code>\n"
            "Precio Cierre SL: <code>590.5678</code>\n\n"
            "📉 <b>Pérdida del Trade: -10.25 USDT</b>\n"
            "💰 Balance de Cuenta Actual: <code>989.75 USDT</code>"
        )
        # Para usar HTML, cambia parse_mode en send_telegram_message_async a ParseMode.HTML

        # Para este ejemplo, enviaremos un mensaje simple sin formato complejo para probar la conexión.
        # Si quieres probar el formato, descomenta el test_message_html y ajusta el parse_mode
        # en la función send_telegram_message_async.
        
        simple_test_message = "Prueba de conexión desde telegram_utils.py (sin formato especial)."

        if send_telegram_message(simple_test_message, TEST_BOT_TOKEN, TEST_CHAT_ID):
            logger.info("Mensaje de prueba enviado exitosamente.")
        else:
            logger.error("Fallo al enviar el mensaje de prueba.")

        # Prueba con un mensaje con un poco de MarkdownV2 (asegúrate que tu bot lo soporte)
        # Recuerda que si cambias parse_mode a HTML en la función, este mensaje no se verá como esperas.
        markdown_test_message = "Esta es una *prueba* con _MarkdownV2_ desde `telegram_utils.py`\.\nDebe escapar caracteres especiales como \. \(ejemplo\)\."
        if send_telegram_message(markdown_test_message, TEST_BOT_TOKEN, TEST_CHAT_ID):
             logger.info("Mensaje de prueba MarkdownV2 enviado exitosamente.")
        else:
             logger.error("Fallo al enviar el mensaje de prueba MarkdownV2.")