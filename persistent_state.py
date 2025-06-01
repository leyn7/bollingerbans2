import json
import os
import logging
from decimal import Decimal, InvalidOperation # Asegúrate que InvalidOperation esté si la usas en alguna conversión
from datetime import datetime
from typing import Optional
import pandas as pd # Para isinstance(obj, pd.Timestamp)

# Importar config para el STATE_FILE_PATH por defecto
import config

logger = logging.getLogger(__name__)

def convert_to_json_serializable(obj):
    """
    Recorre recursivamente un objeto (diccionario o lista) y convierte
    valores Decimal y Timestamp/datetime a strings.
    """
    if isinstance(obj, list):
        return [convert_to_json_serializable(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: convert_to_json_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        return str(obj)
    elif isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj

class PersistentState:
    def __init__(self, filepath=None): # Permitir None para usar el de config
        self.filepath = filepath if filepath else getattr(config, 'STATE_FILE_PATH', 'bot_trading_state.json')
        self.active_trades = {} # Claves: "SYMBOL_SIDE", ej: "BTCUSDT_LONG"
        self.accumulated_losses = {} # ✅ NUEVO: Claves: "SYMBOL_SIDE", Valor: Decimal(pérdida_acumulada)
        self.load_state()

    def load_state(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    data = json.load(f)
                
                # Cargar active_trades
                loaded_active_trades = data.get('active_trades', {})
                if isinstance(loaded_active_trades, dict):
                    self.active_trades = loaded_active_trades
                    logger.info(f"Estado de trades activos cargado. {len(self.active_trades)} trades encontrados.")
                    # (Tu logging existente para detalles de trades)
                else:
                    logger.warning(f"Campo 'active_trades' en '{self.filepath}' no es un diccionario. Inicializando active_trades vacío.")
                    self.active_trades = {}

                # ✅ NUEVO: Cargar accumulated_losses
                loaded_accumulated_losses = data.get('accumulated_losses', {})
                if isinstance(loaded_accumulated_losses, dict):
                    self.accumulated_losses = {
                        key: Decimal(str(value)) # Convertir string de vuelta a Decimal
                        for key, value in loaded_accumulated_losses.items()
                        if value is not None # Solo si hay valor
                    }
                    logger.info(f"Estado de pérdidas acumuladas cargado. {len(self.accumulated_losses)} entradas encontradas.")
                else:
                    logger.warning(f"Campo 'accumulated_losses' en '{self.filepath}' no es un diccionario. Inicializando accumulated_losses vacío.")
                    self.accumulated_losses = {}

            except json.JSONDecodeError:
                logger.error(f"Error decodificando JSON desde {self.filepath}: El archivo está corrupto. Inicializando con estado vacío.", exc_info=True)
                self.active_trades = {}
                self.accumulated_losses = {}
            except Exception as e:
                logger.error(f"Error cargando estado desde {self.filepath}: {e}. Inicializando con estado vacío.", exc_info=True)
                self.active_trades = {}
                self.accumulated_losses = {}
        else:
            logger.info(f"No se encontró archivo de estado en '{self.filepath}'. Inicializando con estado vacío.")
            self.active_trades = {}
            self.accumulated_losses = {}

    def save_state(self):
        try:
            serializable_trades = {
                key: convert_to_json_serializable(value)
                for key, value in self.active_trades.items()
                if value is not None
            }
            # ✅ NUEVO: Serializar accumulated_losses (convert_to_json_serializable ya maneja Decimal)
            serializable_accumulated_losses = convert_to_json_serializable(self.accumulated_losses)

            # Guardar todo en un diccionario raíz
            state_to_save = {
                'active_trades': serializable_trades,
                'accumulated_losses': serializable_accumulated_losses
            }

            with open(self.filepath, 'w') as f:
                json.dump(state_to_save, f, indent=4)
            # logger.debug(f"Estado guardado en {self.filepath}") # Quizás demasiado verboso para cada guardado
        except TypeError as te:
            logger.error(f"Error de tipo al serializar estado para guardar en {self.filepath}: {te}", exc_info=True)
        except Exception as e:
            logger.error(f"Error guardando estado en {self.filepath}: {e}", exc_info=True)

    def set_active_trade(self, trade_key: str, trade_details_dict: dict):
        if not isinstance(trade_key, str) or not trade_key:
            logger.error(f"Clave de trade inválida '{trade_key}' proporcionada a set_active_trade.")
            return

        self.active_trades[trade_key] = trade_details_dict
        # ... (tu logging existente para set_active_trade) ...
        logger.info(f"Nuevo trade para {trade_key} configurado. Estado: {trade_details_dict.get('status', 'N/A')}")
        self.save_state()

    def clear_active_trade(self, trade_key: str, reason=""):
        if not isinstance(trade_key, str) or not trade_key:
            logger.error(f"Clave de trade inválida '{trade_key}' proporcionada a clear_active_trade.")
            return
        # ... (tu logging existente para clear_active_trade) ...
        trade_to_clear = self.active_trades.pop(trade_key, None)
        if trade_to_clear:
             logger.info(f"Trade {trade_key} borrado del estado activo. Razón: {reason}")
             self.save_state()
        else:
             logger.debug(f"No se encontró trade activo para la clave '{trade_key}' al intentar borrar.")


    def get_active_trade(self, trade_key: str) -> Optional[dict]:
        if not isinstance(trade_key, str) or not trade_key:
            return None
        return self.active_trades.get(trade_key)

    def get_trade_status(self, trade_key: str) -> Optional[str]:
        trade = self.get_active_trade(trade_key)
        return trade.get("status") if trade else None

    def is_side_busy(self, trade_key: str) -> bool:
        return self.get_active_trade(trade_key) is not None

    def is_position_open(self, trade_key: str) -> bool:
        return self.get_trade_status(trade_key) == "POSITION_OPEN"

    def is_dynamic_pending_order(self, trade_key: str) -> bool:
        return self.get_trade_status(trade_key) == "PENDING_DYNAMIC_LIMIT"

    # ✅ --- NUEVOS MÉTODOS PARA MARTINGALA --- ✅
    def get_accumulated_loss(self, symbol_key: str) -> Decimal:
        """
        Obtiene la pérdida acumulada para un symbol_key específico (ej. "BTCUSDT_LONG").
        Devuelve Decimal('0') si no se encuentra o no hay pérdida.
        """
        if not isinstance(symbol_key, str) or not symbol_key:
            logger.warning("get_accumulated_loss llamado con symbol_key inválido.")
            return Decimal('0')
        
        loss_str = self.accumulated_losses.get(symbol_key)
        if loss_str is not None:
            try:
                # En load_state ya se convierten a Decimal, pero por si acaso se llama antes de un save/load
                return Decimal(str(loss_str)) 
            except InvalidOperation:
                logger.error(f"Valor de pérdida acumulada inválido para {symbol_key}: '{loss_str}'. Devolviendo 0.")
                return Decimal('0')
        return Decimal('0')

    def update_accumulated_loss(self, symbol_key: str, additional_loss_amount: Decimal):
        """
        Añade un monto de pérdida (debe ser positivo) a la pérdida acumulada para un symbol_key.
        Guarda el estado después de actualizar.
        """
        if not isinstance(symbol_key, str) or not symbol_key:
            logger.error("update_accumulated_loss llamado con symbol_key inválido.")
            return
        if not isinstance(additional_loss_amount, Decimal) or additional_loss_amount < Decimal(0):
            logger.error(f"update_accumulated_loss: additional_loss_amount ({additional_loss_amount}) debe ser un Decimal positivo.")
            # Considera no sumar si es negativo o cero, o tomar el valor absoluto
            additional_loss_amount = abs(additional_loss_amount) # Asegurar que sumamos un valor positivo

        current_loss = self.get_accumulated_loss(symbol_key) # Ya devuelve Decimal
        new_total_loss = current_loss + additional_loss_amount
        
        self.accumulated_losses[symbol_key] = new_total_loss # Se guarda como Decimal, se serializa como str
        logger.info(f"Pérdida acumulada para {symbol_key} actualizada a: {new_total_loss} (añadido: {additional_loss_amount})")
        self.save_state()

    def reset_accumulated_loss(self, symbol_key: str):
        """
        Resetea la pérdida acumulada para un symbol_key a Decimal('0').
        Guarda el estado después de resetear.
        """
        if not isinstance(symbol_key, str) or not symbol_key:
            logger.error("reset_accumulated_loss llamado con symbol_key inválido.")
            return
            
        if symbol_key in self.accumulated_losses and self.accumulated_losses[symbol_key] != Decimal('0'):
            self.accumulated_losses[symbol_key] = Decimal('0')
            logger.info(f"Pérdida acumulada para {symbol_key} reseteada a 0.")
            self.save_state()
        elif symbol_key not in self.accumulated_losses:
             self.accumulated_losses[symbol_key] = Decimal('0') # Asegurar que la clave existe si se resetea
             logger.info(f"Pérdida acumulada para {symbol_key} inicializada y reseteada a 0 (no existía previamente).")
             self.save_state()
        # Si ya era 0, no es necesario guardar de nuevo a menos que quieras forzar la existencia de la clave