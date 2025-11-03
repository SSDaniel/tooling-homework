#----------------------------------------------------------
import sys
from datetime import datetime
import asyncio
import websockets #Websocktes 10.4
import logging
import logging.handlers
from datetime import datetime
import ssl
import json
import uuid
import os
from aiohttp import web  # 
#----------------------------------------------------------

#-------------URL base do servidor OCPP externo (MOVE)--------------
EXTERNAL_CSMS_URL = "ws://127.0.0.1:9999/ocpp"      ######################################"wss://cs.prod.use-move.com/ocpp"

#-------------URL base do servidor OCPP externo (TCHARGE)--------------
EXTERNAL_DATA_WS_URL = "ws://localhost:8765"
external_data_ws = None

# --- CONFIGURAÇÕES DE HOST (AGORA PARA AMBOS OS SERVIDORES) --- 
LOCAL_SERVER_HOST = "127.0.0.1"  # IP(FIXO) para o GATEWAY OCPP ################################"192.168.0.14"
LOCAL_SERVER_PORT = 9000            # Porta para o GATEWAY OCPP

# IP(FIXO) para o SERVIDOR DO MEDIDOR (use o IP da sua máquina)
LOCAL_METER_HOST = "127.0.0.1" # IP que o medidor vai enviar ####################################169.254.35.201
LOCAL_METER_PORT = 8000             # Porta que o medidor vai enviar
#------------------------------------------------------------

# --- Dicionários para Gerenciamento Dinâmico de Conexões ---
UPSTREAM_CLIENTS = {}
DOWNSTREAM_CLIENTS = {}
UPSTREAM_TASKS = {}
#------------------------------------------------------------

LEARNED_POWERS_FILE = "learned_powers.json"

# ----------- CONFIGURAÇÕES DE CONTROLE DE DEMANDA -----------

# Limite máximo total do site em Watts (O TETO MÁXIMO DA INSTALAÇÃO)
MAX_TOTAL_POWER_W = 60000.0 

DEFAULT_MAX_POWER_SEED = 3600.0 
MIN_CHARGE_POWER_W = 1380.0 

CHARGE_POINT_STATE = {}
GATEWAY_PENDING_REQUESTS = set()

# ---  Variável Global para Potência do Site(ALIMENTADA PELO MEDIDOR DA IE)---
# Esta variável será atualizada pelo servidor HTTP do medidor
SITE_POWER_STATE = {
    "current_total_W": 0.0, # Potência total atual do site (lida do medidor)
    "last_updated": None    # Timestamp da última leitura
}
#------------------------------------------------------------

# --- Configuração de Log  ---
from logging.handlers import TimedRotatingFileHandler

log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
gateway_log_dir = os.path.join(log_dir, "gateway")
os.makedirs(gateway_log_dir, exist_ok=True)
file_handler = logging.handlers.TimedRotatingFileHandler(
    os.path.join(gateway_log_dir, "gateway.log"), when="midnight", interval=1, backupCount=0, encoding='utf-8', utc=False
)
# Ajusta o padrão do nome do arquivo rotacionado para gateway_YYYY-MM-DD.log
file_handler.suffix = "%Y-%m-%d.log"
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

#------------------------------------------------------------


# --- FUNÇÕES PARA CARREGAR/SALVAR POTÊNCIAS(JSON INSTALADOR) ---
def load_learned_powers(filename=LEARNED_POWERS_FILE):

    if not os.path.exists(filename):
        logging.info(f"Arquivo '{filename}' não encontrado. Iniciando sem potências pré-carregadas.")
        return {}
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict) and all(isinstance(v, (int, float)) for v in data.values()):
                logging.info(f"Carregadas {len(data)} potências máximas aprendidas de '{filename}'.")
                return data
            else:
                logging.error(f"Arquivo '{filename}' contém dados inválidos. Ignorando.")
                return {}
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Erro ao carregar potências de '{filename}': {e}. Ignorando.")
        return {}

def save_learned_powers(data_to_save, filename=LEARNED_POWERS_FILE):

    powers_only_dict = {}
    first_value = next(iter(data_to_save.values()), None)
    if isinstance(first_value, dict): 
        powers_only_dict = {
            cp_id: state.get("learned_max_power", DEFAULT_MAX_POWER_SEED)
            for cp_id, state in data_to_save.items()
        }
    elif isinstance(first_value, (int, float)):
        powers_only_dict = data_to_save
    elif first_value is None and not data_to_save:
         powers_only_dict = {}
    else:
        logging.error(f"Formato inesperado de dados passado para save_learned_powers...")
        return
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(powers_only_dict, f, indent=4)
        logging.info(f"Potências máximas ({len(powers_only_dict)} carregadores) salvas em '{filename}'.")
    except IOError as e:
        logging.error(f"Erro ao salvar potências em '{filename}': {e}.")
    except Exception as e:
         logging.error(f"Erro inesperado ao salvar potências: {e}", exc_info=True)
# ------------------------------------------------------------



# ---  Handler HTTP Assíncrono (aiohttp) ---
async def handle_meter_post(request):
    # Envia o pacote bruto do medidor para o servidor externo simulado
    try:
        post_data_bytes = await request.read()
        dados_brutos_str = post_data_bytes.decode('utf-8')
        try:
            pacote_json = json.loads(dados_brutos_str)
        except Exception:
            pacote_json = dados_brutos_str
        pacote_envio = {
            "source": "medidor",
            "type": "medidor_raw",
            "data": pacote_json,
            "timestamp": datetime.now().isoformat()
        }
        asyncio.create_task(send_data_to_external_ws(pacote_envio))
    except Exception as e:
        logging.error(f"[EXTERNAL_DATA_WS] Falha ao enviar pacote bruto do medidor: {e}")
    
    # O aiohttp já filtra a rota, então só é necessário processar
    try:
        # Lê o corpo da requisição (assíncrono)
        post_data_bytes = await request.read()
        dados_brutos_str = post_data_bytes.decode('utf-8')

        # Loga no logger principal
        logging.info(f"[METER_SERVER] Pacote recebido: {dados_brutos_str}")

        # Salva o pacote recebido em formato JSONL diário na pasta correta, incluindo timestamp
        medidor_log_dir = os.path.join("logs", "medidor")
        os.makedirs(medidor_log_dir, exist_ok=True)
        jsonl_filename = os.path.join(medidor_log_dir, f"medidor_{datetime.now().strftime('%Y-%m-%d')}.jsonl")
        try:
            # Adiciona timestamp ao pacote
            pacote = json.loads(dados_brutos_str)
            pacote["timestamp"] = datetime.now().isoformat()
            with open(jsonl_filename, "a", encoding="utf-8") as f_jsonl:
                f_jsonl.write(json.dumps(pacote, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.error(f"[METER_SERVER] Falha ao salvar pacote no JSONL: {e}")

        # Tenta extrair a Potência Total ("pt")
        try:
            data_json = json.loads(dados_brutos_str)
            potencia_total_str = data_json.get("pt")
            if potencia_total_str is not None:
                # --- ATUALIZA A VARIÁVEL GLOBAL ---
                SITE_POWER_STATE["current_total_W"] = float(potencia_total_str)
                SITE_POWER_STATE["last_updated"] = datetime.now()
                logging.info(f"[METER_SERVER] Potência total do site atualizada: {SITE_POWER_STATE['current_total_W']:.2f}W")
                # ----------------------------------
            else:
                logging.warning("[METER_SERVER] Pacote JSON recebido, mas chave 'pt' não encontrada.")
        except json.JSONDecodeError:
            logging.error(f"[METER_SERVER] Erro: Pacote recebido não é JSON válido: {dados_brutos_str}")
            return web.Response(status=400, text="Bad Request: Invalid JSON")

        # Responde 200 OK
        return web.Response(text="OK")

    except Exception as e:
        logging.error(f"[METER_SERVER] Erro ao processar requisição: {e}", exc_info=True)
        return web.Response(status=500, text="Internal Server Error")
# --- FIM DO NOVO HANDLER HTTP ---



# --- Lógica do Servidor Local  ---
async def local_server_handler(websocket):
    # Envio de todos os pacotes recebidos do carregador para o servidor externo simulado
    async def enviar_pacote_bruto_carregador(msg_str):
        try:
            try:
                pacote_json = json.loads(msg_str)
            except Exception:
                pacote_json = msg_str
            pacote_envio = {
                "source": "carregador",
                "type": "carregador_raw",
                "charge_point_id": charge_point_id,
                "data": pacote_json,
                "timestamp": datetime.now().isoformat()
            }
            asyncio.create_task(send_data_to_external_ws(pacote_envio))
        except Exception as e:
            logging.error(f"[EXTERNAL_DATA_WS] Falha ao enviar pacote bruto do carregador: {e}")
    path = websocket.path
    charge_point_id = path.strip('/')
    if not charge_point_id:
        logging.warning(f"[Local Server] Conexão recebida sem Charge Point ID no path.")
        await websocket.close(1011, "Charge Point ID não especificado no path")
        return
    if charge_point_id not in CHARGE_POINT_STATE:
        initial_max_power = loaded_learned_powers.get(charge_point_id, DEFAULT_MAX_POWER_SEED)
        if charge_point_id in loaded_learned_powers:
             power_source_log = "carregado do arquivo"
        else:
             power_source_log = f"padrão ({DEFAULT_MAX_POWER_SEED}W)"
             loaded_learned_powers[charge_point_id] = DEFAULT_MAX_POWER_SEED
             save_learned_powers(loaded_learned_powers, filename=LEARNED_POWERS_FILE) 
        logging.info(f"[Local Server] Carregador '{charge_point_id}' detectado. Usando {initial_max_power:.0f}W ({power_source_log}) como máximo inicial.")
        CHARGE_POINT_STATE[charge_point_id] = {
            "status": "Available", "current_power_W": 0.0,
            "learned_max_power": initial_max_power, "current_limit_W": initial_max_power,
            "message_buffer": []
        }
    else:
        logging.info(f"[Local Server] Carregador '{charge_point_id}' (Max: {CHARGE_POINT_STATE[charge_point_id]['learned_max_power']}W) reconectado.")
        CHARGE_POINT_STATE[charge_point_id]["status"] = "Available"
        logging.info(f"[Local Server] O limite de potência anterior ({CHARGE_POINT_STATE[charge_point_id]['current_limit_W']:.0f}W) foi mantido para '{charge_point_id}'.")
        if "message_buffer" not in CHARGE_POINT_STATE[charge_point_id]:
             CHARGE_POINT_STATE[charge_point_id]["message_buffer"] = []
    DOWNSTREAM_CLIENTS[charge_point_id] = websocket
    task = UPSTREAM_TASKS.get(charge_point_id)
    if task is None or task.done():
        if task and task.done():
            logging.warning(f"[Local Server] Tarefa externa para '{charge_point_id}' estava 'done'. Reiniciando.")
            if task.exception():
                logging.error(f"[Local Server] Exceção na tarefa anterior: {task.exception()}")
        logging.info(f"[Local Server] Iniciando conexão externa para '{charge_point_id}'...")
        new_task = asyncio.create_task(external_client_handler(charge_point_id))
        UPSTREAM_TASKS[charge_point_id] = new_task
    else:
        logging.info(f"[Local Server] Conexão externa para '{charge_point_id}' já está ativa.")
    try:
        async for message in websocket:
            # Envia o pacote bruto do carregador para o servidor externo simulado
            await enviar_pacote_bruto_carregador(message)
            try:
                msg_json = json.loads(message)
                msg_type_id = msg_json[0]
                if charge_point_id not in CHARGE_POINT_STATE: continue
                state = CHARGE_POINT_STATE[charge_point_id]
                if msg_type_id == 3:
                    msg_id = msg_json[1]
                    if msg_id in GATEWAY_PENDING_REQUESTS:
                        logging.info(f"[GATEWAY CONSUME {charge_point_id}]: Resposta recebida para '{msg_id}'. Mensagem consumida (não encaminhada).")
                        GATEWAY_PENDING_REQUESTS.discard(msg_id)
                        continue
                msg_action = msg_json[2]
                if msg_action == "StatusNotification":
                    payload = msg_json[3]
                    new_status = payload.get("status")
                    if new_status:
                        old_status = state["status"]
                        state["status"] = new_status
                        logging.info(f"[STATE UPDATE {charge_point_id}]: Status alterado de '{old_status}' para '{new_status}'")
                        if old_status == "Charging" and new_status not in ["Charging", "SuspendedEV"]:
                            logging.info(f"[CONTROL {charge_point_id}] Carga finalizada (Status: {new_status}). Removendo limitação DESTE carregador.")
                            asyncio.create_task(send_charging_profile(charge_point_id, state["learned_max_power"]))
                elif msg_action == "MeterValues":
                    payload = msg_json[3]
                    values = payload.get("meterValue", [{}])[0].get("sampledValue", [])
                    current_power = 0.0
                    found = False
                    for v in values:
                        if v.get("measurand") == "Power.Active.Import":
                            current_power = float(v.get("value", 0))
                            if v.get("unit") == "kW":
                                current_power *= 1000.0
                            found = True
                            break
                    if found:
                        state["current_power_W"] = current_power
                        logging.info(f"[STATE UPDATE {charge_point_id}]: Potência atual: {current_power:.2f}W")
                        if current_power > 500 and state["status"] not in ["Charging", "SuspendedEV", "SuspendedEVSE"]:
                             logging.warning(f"[STATE INFERENCE {charge_point_id}] Potência detectada ({current_power:.0f}W) mas status era '{state['status']}'. Forçando para 'Charging'.")
                             state["status"] = "Charging"
                        elif current_power <= 500 and state["status"] == "Charging":
                             logging.warning(f"[STATE INFERENCE {charge_point_id}] Potência caiu para {current_power:.0f}W enquanto status era 'Charging'. Forçando para 'Available'.")
                             state["status"] = "Available"
                             logging.info(f"[CONTROL {charge_point_id}] Carga inferida como finalizada. Removendo limitação DESTE carregador.")
                             asyncio.create_task(send_charging_profile(charge_point_id, state["learned_max_power"]))
                        if current_power > (state["learned_max_power"] * 1.01):
                            logging.warning(f"[LEARNING {charge_point_id}]: Novo máximo aprendido! De {state['learned_max_power']:.0f}W para {current_power:.0f}W")
                            state["learned_max_power"] = current_power
                            state["current_limit_W"] = current_power
                            save_learned_powers(CHARGE_POINT_STATE)
            except Exception as e:
                logging.warning(f"[PARSER {charge_point_id}]: Erro ao processar mensagem JSON: {e} - Mensagem: {message}")
                continue
            logging.info(f"[FROM CHARGER {charge_point_id}]: {message}")
            upstream_socket = UPSTREAM_CLIENTS.get(charge_point_id)
            if upstream_socket and not upstream_socket.closed:
                await upstream_socket.send(message)
                logging.info(f"[TO EXTERNAL SERVER FOR {charge_point_id}]: Mensagem encaminhada.")
            else:
                logging.warning(f"[BUFFERING {charge_point_id}] Conexão externa indisponível. Armazenando mensagem no buffer.")
                if charge_point_id in CHARGE_POINT_STATE:
                    CHARGE_POINT_STATE[charge_point_id]["message_buffer"].append(message)
                else:
                    logging.error(f"[BUFFERING {charge_point_id}] ERRO: Estado não existe mais. Mensagem descartada.")
    except websockets.exceptions.ConnectionClosed as e:
        logging.info(f"[Local Server] Conexão com o carregador '{charge_point_id}' fechada: {e.reason or 'Desconexão abrupta'}")
    except Exception as e:
        logging.error(f"[Local Server] Erro inesperado no handler do carregador '{charge_point_id}': {e}", exc_info=True)
    finally:
        if charge_point_id in DOWNSTREAM_CLIENTS:
            del DOWNSTREAM_CLIENTS[charge_point_id]
        if charge_point_id in CHARGE_POINT_STATE:
            CHARGE_POINT_STATE[charge_point_id]["status"] = "Offline"
        logging.info(f"[Local Server] Cliente '{charge_point_id}' desconectado e removido.")
        logging.info(f"[Gateway] Propagando desconexão para o servidor externo de '{charge_point_id}'...")
        task = UPSTREAM_TASKS.pop(charge_point_id, None) 
        if task and not task.done():
            task.cancel()
            logging.info(f"[Gateway] Tarefa de conexão externa para '{charge_point_id}' foi cancelada.")
        else:
            logging.info(f"[Gateway] Nenhuma tarefa externa ativa encontrada para '{charge_point_id}'.")

# ------------------------------------------------------------
# --- WebSocket para envio de dados para servidor externo TCHARGE/UFPB ---

async def connect_external_data_ws():
    global external_data_ws
    while True:
        try:
            external_data_ws = await websockets.connect(EXTERNAL_DATA_WS_URL)
            logging.info(f"[EXTERNAL_DATA_WS] Conectado ao servidor externo de dados: {EXTERNAL_DATA_WS_URL}")
            while True:
                await asyncio.sleep(1)
                if external_data_ws.closed:
                    break
        except Exception as e:
            logging.error(f"[EXTERNAL_DATA_WS] Erro na conexão: {e}")
        await asyncio.sleep(5)

async def send_data_to_external_ws(data):
    if external_data_ws and not external_data_ws.closed:
        try:
            await external_data_ws.send(json.dumps(data))
            logging.info(f"[EXTERNAL_DATA_WS] Dados enviados: {data}")
        except Exception as e:
            logging.error(f"[EXTERNAL_DATA_WS] Falha ao enviar dados: {e}")
    else:
        logging.warning("[EXTERNAL_DATA_WS] WebSocket não conectado. Dados não enviados.")


# --- Lógica do Cliente Externo  ---
async def external_client_handler(charge_point_id):
    url = f"{EXTERNAL_CSMS_URL}/{charge_point_id}"
    ssl_context = None
    if EXTERNAL_CSMS_URL.startswith("wss://"):
        ssl_context = ssl._create_unverified_context()
    while True:
        try:
            logging.debug(f"[External Client] Tentando conectar a: {url}")
            async with websockets.connect(
                url, subprotocols=["ocpp1.6"], ssl=ssl_context,
                extra_headers={"User-Agent": "Gateway-TCharge-Python"}, open_timeout=10
            ) as websocket:
                logging.info(f"[External Client] Conectado ao servidor externo como '{charge_point_id}'")
                UPSTREAM_CLIENTS[charge_point_id] = websocket
                try:
                    if charge_point_id in CHARGE_POINT_STATE:
                        buffer = CHARGE_POINT_STATE[charge_point_id].get("message_buffer", [])
                        if buffer:
                            logging.info(f"[BUFFER FLUSH {charge_point_id}] Conexão externa pronta. Enviando {len(buffer)} mensagens pendentes (FIFO)...")
                            messages_to_flush = list(buffer)
                            CHARGE_POINT_STATE[charge_point_id]["message_buffer"].clear()
                            
                            for buffered_msg in messages_to_flush:
                                await websocket.send(buffered_msg) # Envia para o servidor externo
                                logging.info(f"[BUFFER SEND {charge_point_id} via FLUSH]: {buffered_msg}")
                            logging.info(f"[BUFFER FLUSH {charge_point_id}] Buffer limpo.")
                except Exception as e:
                    logging.error(f"[BUFFER FLUSH {charge_point_id}] Erro ao enviar buffer: {e}. Mensagens podem ter sido perdidas.")
                
                async for message in websocket:
                    logging.info(f"[FROM EXTERNAL SERVER FOR {charge_point_id}]: {message}")
                    downstream_socket = DOWNSTREAM_CLIENTS.get(charge_point_id)
                    if downstream_socket and not downstream_socket.closed:
                        await downstream_socket.send(message)
                        logging.info(f"[TO CHARGER {charge_point_id}]: Mensagem encaminhada.")
                    else:
                        logging.warning(f"[BUFFERING {charge_point_id}] Carregador local offline. Verificando prioridade...")
                        is_stop_command = False
                        try:
                            msg_json = json.loads(message)
                            if msg_json[0] == 2 and msg_json[2] == "RemoteStopTransaction":
                                is_stop_command = True
                        except Exception:
                            pass 
                        if charge_point_id in CHARGE_POINT_STATE:
                            buffer = CHARGE_POINT_STATE[charge_point_id].get("message_buffer", [])
                            if is_stop_command:
                                buffer.insert(0, message)
                                logging.warning(f"[PRIORITY BUFFER {charge_point_id}] Comando RemoteStopTransaction armazenado com PRIORIDADE.")
                            else:
                                buffer.append(message)
                                logging.warning(f"[BUFFERING {charge_point_id}] Mensagem normal armazenada no buffer.")
                        else:
                            logging.error(f"[BUFFERING {charge_point_id}] ERRO: Estado não existe mais. Mensagem descartada.")
        except asyncio.CancelledError:
            logging.info(f"[External Client] Tarefa para '{charge_point_id}' cancelada (carregador local desconectou). Encerrando.")
            break 
        except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, asyncio.TimeoutError) as e:
            logging.error(f"[External Client] Conexão com servidor externo para '{charge_point_id}' perdida: {e}...", exc_info=False)
        except Exception as e:
            logging.error(f"[External Client] Erro inesperado para '{charge_point_id}': {e}...", exc_info=True)
        finally:
            if charge_point_id in UPSTREAM_CLIENTS:
                del UPSTREAM_CLIENTS[charge_point_id]
        if not asyncio.current_task().cancelled():
            logging.info(f"[External Client] '{charge_point_id}' desconectado. Aguardando 10s para reconectar.")
            await asyncio.sleep(10)
    logging.info(f"[External Client] Tarefa para '{charge_point_id}' finalizada.")
# ------------------------------------------------------------



# --- FUNÇÕES DE CONTROLE OCPP (send_trigger_message, send_charging_profile) ---
async def send_trigger_message(cp_id, message_name="MeterValues"):
    socket = DOWNSTREAM_CLIENTS.get(cp_id)
    if not socket or socket.closed:
        return
    message_id = str(uuid.uuid4())
    payload = {"requestedMessage": message_name}
    message = [2, message_id, "TriggerMessage", payload]
    try:
        await socket.send(json.dumps(message))
        GATEWAY_PENDING_REQUESTS.add(message_id)
        logging.info(f"[TO CHARGER {cp_id}]: Solicitando {message_name}...")
    except Exception as e:
        logging.error(f"[TRIGGER] Erro ao enviar TriggerMessage para '{cp_id}': {e}")

async def send_charging_profile(cp_id, limit_in_watts):
    socket = DOWNSTREAM_CLIENTS.get(cp_id)
    if not socket or socket.closed:
        logging.warning(f"[CONTROL] Carregador '{cp_id}' não conectado. Impossível definir limite.")
        return
    limit_in_watts = round(max(0.0, limit_in_watts), 2)
    if cp_id in CHARGE_POINT_STATE:
         CHARGE_POINT_STATE[cp_id]["current_limit_W"] = limit_in_watts
    message_id = str(uuid.uuid4())
    payload = {
        "connectorId": 0, 
        "csChargingProfiles": {
            "chargingProfileId": 9901, "stackLevel": 1, 
            "chargingProfilePurpose": "ChargePointMaxProfile", 
            "chargingProfileKind": "Recurring", "recurrencyKind": "Daily",
            "chargingSchedule": {
                "duration": 86400, "startSchedule": "2025-01-01T00:00:00Z", 
                "chargingRateUnit": "W", 
                "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit_in_watts}]
            }
        }
    }
    message = [2, message_id, "SetChargingProfile", payload]
    try:
        await socket.send(json.dumps(message))
        GATEWAY_PENDING_REQUESTS.add(message_id)
        logging.info(f"[TO CHARGER {cp_id}]: Enviando SetChargingProfile (MaxProfile), limite: {limit_in_watts}W")
    except Exception as e:
        logging.error(f"[CONTROL] Erro ao enviar SetChargingProfile para '{cp_id}': {e}")
# ------------------------------------



# --- LOOP DE SOLICITAÇÃO DE MEDIDORES ---
async def request_meter_values_loop():
    while True:
        try:
            active_chargers = [
                cp_id for cp_id, state in CHARGE_POINT_STATE.items() 
                if state["status"] == "Charging"
            ]
            for cp_id in active_chargers:
                await send_trigger_message(cp_id, "MeterValues")
        except Exception as e:
            logging.error(f"[METER_LOOP] Erro no loop de pedido de MeterValues: {e}")          
        await asyncio.sleep(60) # 1 minuto
# ------------------------------------------------------------



# --- CÉREBRO DE CONTROLE DE DEMANDA  ---
async def demand_control_loop():
    # Espera inicial para dar tempo aos carregadores se conectarem e enviarem dados.
    await asyncio.sleep(10) 
    
    # Loop infinito que mantém o controle ativo.
    while True:
        try:
            # --- 1. COLETA DE DADOS ---
            
            # Pega um snapshot seguro do estado atual
            current_state_snapshot = CHARGE_POINT_STATE.copy()

            # Cria um dicionário temporário ('charging_chargers') contendo APENAS
            # os carregadores que estão ATUALMENTE no estado "Charging".
            charging_chargers = {
                cp_id: state for cp_id, state in current_state_snapshot.items()
                if state.get("status") == "Charging"
            }
            
            # --- CONTAGEM DE CARREGADORES EM ESPERA ---
            # (Calculado apenas para fins de log)
            connected_chargers_count = sum(1 for state in current_state_snapshot.values() if state.get("status") != "Offline")
            waiting_chargers_count = connected_chargers_count - len(charging_chargers)
            
            # ---  CÁLCULO DA POTÊNCIA DISPONÍVEL ---
            
            # Lê a potência total atual do site (do medidor HTTP)
            current_site_power_W = SITE_POWER_STATE.get("current_total_W", 0.0)
            
            # Calcula a demanda ATUAL (real) apenas dos carregadores
            total_charger_demand_W = sum(state["current_power_W"] for state in charging_chargers.values())
            
            # Calcula a potência que NÃO VEM dos carregadores (consumo da "casa")
            non_charger_site_power_W = max(0, current_site_power_W - total_charger_demand_W)
            
            # A potência disponível para o GRUPO de carregadores é o limite do site
            # menos o consumo da "casa".
            available_power_for_CHARGER_GROUP_W = MAX_TOTAL_POWER_W - non_charger_site_power_W

            # Segurança: Garante que a potência disponível não seja negativa
            if available_power_for_CHARGER_GROUP_W < 0:
                logging.warning(f"[CONTROL] Consumo do site (excluindo carregadores) ({non_charger_site_power_W:.0f}W) "
                                f"excede o limite total ({MAX_TOTAL_POWER_W:.0f}W). "
                                f"Potência para carregadores definida como 0.")
                available_power_for_CHARGER_GROUP_W = 0 
            
            # --- Log informativo mostrando a situação atual ---
            logging.info(
                f"[CONTROL] Demanda (Carreg.): {total_charger_demand_W:.2f}W / {available_power_for_CHARGER_GROUP_W:.0f}W (Disponível p/ Carregadores) | "
                f"Ativos: {len(charging_chargers)} | "
                f"Espera: {waiting_chargers_count} | "
                f"Consumo Total Site: {current_site_power_W:.0f}W | "
                f"Consumo Outros: {non_charger_site_power_W:.0f}W"
            )


            tasks_to_run = []


            if charging_chargers:
                
                total_learned_max_power_active = sum(state.get("learned_max_power", DEFAULT_MAX_POWER_SEED) for state in charging_chargers.values())

                if total_learned_max_power_active > 0:
                    
                    # Verifica se há sobrecarga REAL
                    is_overload = total_charger_demand_W > available_power_for_CHARGER_GROUP_W
                    
                    # --- ALTERAÇÃO AQUI: Log condicional ---
                    # Log de aviso SÓ se houver sobrecarga
                    if is_overload:
                        logging.warning(f"[CONTROL] SOBRECARGA! ⚡ Demanda: {total_charger_demand_W:.2f}W > Disponível: {available_power_for_CHARGER_GROUP_W:.0f}W. Aplicando balanceamento.")
                    # --- FIM DA ALTERAÇÃO ---
                    
                    log_details = [] # Lista para o novo log de resumo
                    
                    for cp_id, state in charging_chargers.items():
                        my_share_percent = state.get("learned_max_power", DEFAULT_MAX_POWER_SEED) / total_learned_max_power_active
                        new_limit_W = available_power_for_CHARGER_GROUP_W * my_share_percent
                        new_limit_W = max(new_limit_W, MIN_CHARGE_POWER_W)
                        new_limit_W = min(new_limit_W, state.get("learned_max_power", DEFAULT_MAX_POWER_SEED))
                        
                        # Envia o comando se o limite for diferente (com persistência se houver sobrecarga real)
                        # (1% de tolerância)
                        if abs(new_limit_W - state.get("current_limit_W", 0)) > (new_limit_W * 0.01) or is_overload:
                            tasks_to_run.append(send_charging_profile(cp_id, new_limit_W))
                            log_details.append(f"{cp_id}: {new_limit_W:.0f}W") # Adiciona ao resumo
                            
                else:
                    logging.error("[CONTROL] Carregadores ativos detectados, mas 'total_learned_max_power_active' é zero. Não é possível balancear.")
            
            # --- 5. EXECUTAR TODOS OS COMANDOS ---
            if tasks_to_run:
                logging.info(f"Enviando {len(tasks_to_run)} atualizações de perfil de carga...")
                await asyncio.gather(*tasks_to_run) 
                
                # Log de resumo APÓS o envio
                if log_details:
                     logging.info(f"[CONTROL] Limites aplicados: {' | '.join(log_details)}")

        except Exception as e:
            logging.error(f"[CONTROL_LOOP] Erro no loop de controle de demanda: {e}", exc_info=True)

        # Pausa a execução desta tarefa por 10 segundos antes de verificar tudo de novo.
        await asyncio.sleep(10)


# --- Função Principal ---
async def main():
    # Inicia conexão WebSocket com servidor externo de dados
    asyncio.create_task(connect_external_data_ws())
    logging.info("Iniciando o Gateway OCPP e o Servidor do Medidor...")
    
    # --- 1. Configurar Servidor do Medidor (aiohttp) ---
    app = web.Application()
    # Adiciona a rota POST que o medidor usará
    app.router.add_post("/api/insert.php", handle_meter_post) 
    runner = web.AppRunner(app)
    await runner.setup()
    # Inicia o servidor na porta 8000
    meter_server = web.TCPSite(runner, LOCAL_METER_HOST, LOCAL_METER_PORT)
    
    # --- 2. Configurar Servidor do Gateway (websockets) ---
    gateway_server = await websockets.serve(
        local_server_handler,
        LOCAL_SERVER_HOST,
        LOCAL_SERVER_PORT
    )
    
    logging.info(f"Gateway OCPP escutando em ws://{LOCAL_SERVER_HOST}:{LOCAL_SERVER_PORT}")
    logging.info(f"Servidor do Medidor escutando em http://{LOCAL_METER_HOST}:{LOCAL_METER_PORT}/api/insert.php")
    
    # --- 3. Iniciar Loops de Controle (como antes) ---
    logging.info("Iniciando loops de controle de demanda e medição...")
    asyncio.create_task(demand_control_loop())
    asyncio.create_task(request_meter_values_loop())
    
    # --- 4. Iniciar os servidores e esperar ---
    await meter_server.start() # Inicia o servidor http
    logging.info("Servidor do Medidor iniciado.")
    
    # Espera o servidor do gateway fechar 
    await gateway_server.wait_closed() 
    
    # Limpeza (se o gateway fechar)
    await runner.cleanup()
    logging.info("Servidores limpos.")

# --- Bloco de Inicialização  ---
if __name__ == "__main__":
    # Carrega as potências salvas ANTES de iniciar qualquer coisa
    loaded_learned_powers = load_learned_powers()
    
    loop = None
    try:
       
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
        loop.run_forever() # Isso será interrompido pelo KeyboardInterrupt

    except KeyboardInterrupt:
        logging.info("Gateway desligando (Ctrl+C)... Removendo limitações de potência.")
        
        if loop and loop.is_running() and DOWNSTREAM_CLIENTS:
            shutdown_tasks = []
            connected_ids = list(DOWNSTREAM_CLIENTS.keys()) 
            logging.info(f"Encontrados {len(connected_ids)} carregadores conectados para liberar.")
            
            for cp_id in connected_ids:
                if cp_id in CHARGE_POINT_STATE:
                    max_power = CHARGE_POINT_STATE[cp_id].get("learned_max_power", DEFAULT_MAX_POWER_SEED)
                    logging.info(f"Agendando liberação para '{cp_id}' (Limite: {max_power}W)")
                    task = loop.create_task(send_charging_profile(cp_id, max_power))
                    shutdown_tasks.append(task)
            
            if shutdown_tasks:
                try:
                    logging.info("Aguardando envio dos perfis de liberação (timeout 5s)...")
                    # Damos 5 segundos para as tarefas de desligamento rodarem
                    loop.run_until_complete(asyncio.wait_for(
                        asyncio.gather(*shutdown_tasks, return_exceptions=True),
                        timeout=5.0
                    ))
                    logging.info("Envio dos perfis de liberação concluído (ou timeout).")
                except asyncio.TimeoutError:
                    logging.warning("Timeout de 5s atingido durante a liberação dos carregadores.")
                except Exception as e:
                     logging.error(f"Erro inesperado durante o envio dos perfis de liberação: {e}")
            else:
                logging.info("Nenhum perfil de liberação para enviar.")
        else:
            logging.info("Nenhum carregador conectado para liberar ou loop não está rodando.")
            
    finally:
        # Limpeza final do loop asyncio
        if loop and loop.is_running():
            logging.info("Fechando o loop de eventos asyncio...")
            loop.close()
        logging.info("Gateway desligado.")