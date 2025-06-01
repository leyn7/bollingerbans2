# binance_client_setup.py
import logging
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config 

logger = logging.getLogger(__name__)

class BinanceClientSetup:
    def __init__(self, api_key: str, api_secret: str):
        if not api_key or not api_secret:
            msg = "API Key y API Secret son obligatorios para inicializar el cliente de Binance."
            logger.error(msg)
            raise ValueError(msg)
        
        self.client = Client(api_key, api_secret)
        logger.info("Cliente de Binance inicializado.")
        self._test_connectivity()

    def _test_connectivity(self):
        try:
            server_time = self.client.futures_time()
            logger.info(f"Conectividad con Binance Futures API exitosa. Hora del servidor: {server_time.get('serverTime')}")
        except BinanceAPIException as e:
            logger.error(f"Error de API probando conectividad con Binance Futures: {e}", exc_info=True)
            raise 
        except Exception as e:
            logger.error(f"Error inesperado probando conectividad con Binance Futures: {e}", exc_info=True)
            raise

    def get_client(self) -> Client:
        return self.client

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - [%(module)s.%(funcName)s:%(lineno)d] - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    logger.info("Ejecutando prueba de binance_client_setup.py...")
    
    if not config.API_KEY or not config.API_SECRET:
        logger.error("Variables de entorno API no configuradas.")
    else:
        try:
            client_handler = BinanceClientSetup(config.API_KEY, config.API_SECRET)
            client_instance = client_handler.get_client()
            logger.info("Prueba de BinanceClientSetup completada exitosamente.")
        except Exception as e:
            logger.error(f"Error durante la prueba: {e}", exc_info=True)