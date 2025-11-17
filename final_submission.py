import streamlit as st
import pandas as pd
import plotly.graph_objs as go
import plotly.express as px
import re
from datetime import datetime, timedelta
import numpy as np
import os
from html import escape
from collections import deque

# --- Configuração da Página (DEVE SER O 1º COMANDO STREAMLIT) ---
st.set_page_config(layout="wide")

# --- Constantes ---
CHARGER_MAX_POWER = {
    "125020001113": 7500.0,
    "125020001122": 7500.0,
    "125020001148": 7500.0,
    "125020001128": 7500.0,
    "0000324070000979": 30000.0,
    "0000324070001003": 30000.0
}
KNOWN_SERIALS = set(CHARGER_MAX_POWER.keys())


@st.cache_data
def parse_log(log_file):
    chargers = {}
    status_events = {}
    control_events = []
    all_times = set()
    statusnotif_re = re.compile(r'\[FROM CHARGER ([^]]+)\]:.*StatusNotification.*"status"\s*:\s*"([A-Za-z]+)"')
    power_stateupdate_re = re.compile(r'\[STATE UPDATE ([^]]+)\]: Potência atual: ([\d.]+)W')
    control_applied_re = re.compile(r"\[CONTROL\] SOBRECARGA!.*Aplicando balanceamento\.")
    site_power_re = re.compile(r'Potência total do site atualizada: ([\d.]+)W')
    site_power_events = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                ts_str = line.split(" - ")[0]
                timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
            except Exception:
                continue
            all_times.add(timestamp)
            m_statusnotif = statusnotif_re.search(line)
            if m_statusnotif:
                cp_id = m_statusnotif.group(1).strip()
                status = m_statusnotif.group(2)
                if cp_id not in status_events:
                    status_events[cp_id] = []
                status_events[cp_id].append({
                    "timestamp": timestamp,
                    "status": status
                })
            m_power = power_stateupdate_re.search(line)
            if m_power:
                cp_id = m_power.group(1).strip()
                power = float(m_power.group(2))
                if cp_id not in chargers:
                    chargers[cp_id] = []
                chargers[cp_id].append({
                    "timestamp": timestamp,
                    "power": power
                })
            m_site_power = site_power_re.search(line)
            if m_site_power:
                site_power = float(m_site_power.group(1))
                site_power_events.append({
                    "timestamp": timestamp,
                    "power": site_power
                })
            if control_applied_re.search(line):
                control_events.append(timestamp)
    def is_serial(cp_id):
        return cp_id and not cp_id.startswith("EXTERNAL SERVER") and cp_id.replace(' ', '').isalnum()
    serial_chargers = {cp_id: events for cp_id, events in chargers.items() if is_serial(cp_id)}
    serial_status = {cp_id: events for cp_id, events in status_events.items() if is_serial(cp_id)}
    return serial_chargers, serial_status, control_events, sorted(all_times), site_power_events

@st.cache_data
def get_disconnects(log_file):
    """
    Varre o log e retorna um dicionário por carregador contendo contagens de
    desconexões locais e desconexões externas (servidor externo).

    Regras:
      - Conta como desconexão local: [Local Server] Cliente ... desconectado e removido.
      - Conta como desconexão externa: [External Client] Conexão com servidor externo ... perdida
      - Se uma desconexão local for seguida (até 60s) por eventos de gateway/external/cancel, conta apenas como uma desconexão (ignora as consequências).
      - Não conta como desconexão: gateway, cancelada, external_disconnected (se vier logo após external_lost)
    """
    import re
    from datetime import datetime
    local_re = re.compile(r"\[Local Server\] Cliente '([^']+)' desconectado e removido\.")
    external_conn_lost_re = re.compile(r"\[External Client\] Conexão com servidor externo para '([^']+)' perdida")

    events = []  # (timestamp, cp_id, type, line)
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                ts_str = line.split(" - ")[0]
                timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
            except Exception:
                continue
            m = local_re.search(line)
            if m:
                cp_id = m.group(1)
                events.append((timestamp, cp_id, 'local', line))
                continue
            m = external_conn_lost_re.search(line)
            if m:
                cp_id = m.group(1)
                events.append((timestamp, cp_id, 'external_lost', line))
                continue
    # ordenar eventos por tempo
    events.sort(key=lambda x: x[0])

    disconnects = {}
    WINDOW = 60  # segundos
    for cp_id in set(e[1] for e in events):
        disconnects[cp_id] = {'local': {}, 'external': {}, 'events': []}
        evs = [e for e in events if e[1] == cp_id]
        last_local = None
        last_external = None
        for ts, _, ev_type, line in evs:
            disconnects[cp_id]['events'].append((ts, ev_type, line))
            day = ts.date()
            if ev_type == 'local':
                # só conta se não houver outro local nos últimos WINDOW segundos
                if last_local is None or (ts - last_local).total_seconds() > WINDOW:
                    disconnects[cp_id]['local'][day] = disconnects[cp_id]['local'].get(day, 0) + 1
                    last_local = ts
                    last_external = None  # reseta janela externa
            elif ev_type == 'external_lost':
                # só conta se não houver local nos últimos WINDOW segundos
                if last_local is None or (ts - last_local).total_seconds() > WINDOW:
                    if last_external is None or (ts - last_external).total_seconds() > WINDOW:
                        disconnects[cp_id]['external'][day] = disconnects[cp_id]['external'].get(day, 0) + 1
                        last_external = ts
    return disconnects


# --- FUNÇÃO DE VERIFICAÇÃO DE SENHA (IDÊNTICA) ---
def check_password():

    try:
        correct_password = st.secrets["passwords"]["admin_password"]
    except KeyError:
        st.error("Erro: Senha de administrador não configurada nos 'Secrets' do Streamlit.")
        st.stop() 

    password_attempt = st.text_input("Digite a senha para acessar o dashboard:", type="password")

    if not password_attempt:
        st.info("Por favor, digite a senha para continuar.")
        st.stop()

    if password_attempt == correct_password:
        return True
    else:
        st.error("Senha incorreta. Tente novamente.")
        return False

# --- NOVA FUNÇÃO DE PROCESSAMENTO "SEM RAMPAS" (COM 2 PEQUENAS CORREÇÕES) ---
def process_data_no_ramps(df_power_raw, df_status_raw, all_timestamps, selected_serials):
    """
    Processa dados brutos para criar um dataframe denso, minuto a minuto,
    removendo "rampas" ao setar potência para 0 quando status != 'Charging'.
    Baseado na lógica do script 'plot_chargers_and_total_per_day'.
    """
    # Tolerância em minutos (como no seu script original)
    TOLERANCIA_MINUTOS = 2
    
    # --- CORREÇÃO 3: Usar pd.Timedelta para comparação ---
    TOLERANCIA_TIMEDELTA = pd.Timedelta(minutes=TOLERANCIA_MINUTOS)
    
    # Converte para listas de tuplas (timestamp, value) para iteração muito mais rápida
    power_events = {sn: list(df_power_raw[df_power_raw['serial_number'] == sn].sort_values('timestamp')[['timestamp', 'potencia_W']].itertuples(index=False, name=None)) for sn in selected_serials}
    status_events = {sn: list(df_status_raw[df_status_raw['serial_number'] == sn].sort_values('timestamp')[['timestamp', 'status']].itertuples(index=False, name=None)) for sn in selected_serials}
    
    # Índices para percorrer as listas de eventos
    power_idx = {sn: 0 for sn in selected_serials}
    status_idx = {sn: 0 for sn in selected_serials}
    
    # Guarda o último estado conhecido
    last_power = {sn: 0.0 for sn in selected_serials}
    last_status = {sn: "Available" for sn in selected_serials}
    last_power_time = {sn: None for sn in selected_serials}
    
    data = [] # Lista para o novo dataframe "wide"

    # Itera por CADA timestamp único encontrado nos dados
    for t in all_timestamps:
        row = {"timestamp": t}
        
        for sn in selected_serials:
            # 1. Atualiza o Status (avança até o timestamp atual 't')
            sn_statuses = status_events[sn]
            while status_idx[sn] < len(sn_statuses) and sn_statuses[status_idx[sn]][0] <= t:
                last_status[sn] = sn_statuses[status_idx[sn]][1] # (timestamp, status)
                status_idx[sn] += 1
                
            # 2. Atualiza a Potência (só se o timestamp for EXATO)
            sn_powers = power_events[sn]
            if power_idx[sn] < len(sn_powers) and sn_powers[power_idx[sn]][0] == t:
                last_power[sn] = sn_powers[power_idx[sn]][1] # (timestamp, power)
                last_power_time[sn] = t
                power_idx[sn] += 1
            
            # 3. Lógica "no-ramp" (exatamente como no seu script)
            carregando = False
            if last_status[sn] == 'Charging':
                carregando = True
            elif last_power_time[sn] is not None and (t - last_power_time[sn]) <= TOLERANCIA_TIMEDELTA: # <-- CORREÇÃO 3
                carregando = True
                
            current_power = 0.0
            if carregando:
                current_power = last_power[sn]
            else:
                last_power[sn] = 0.0 # Zera a potência "lembrada"
                
            row[sn] = current_power
        
        data.append(row)

    if not data:
        return pd.DataFrame()

    df_dense = pd.DataFrame(data)
    
    # Agrega por minuto (como no seu script original)
    df_dense_min = df_dense.set_index('timestamp').resample('min').mean().reset_index()
    
    # --- CORREÇÃO 2: Verificar se há colunas para "derreter" (melt) ---
    cols_to_melt = [sn for sn in selected_serials if sn in df_dense_min.columns]
    
    if not cols_to_melt:
        # Se não há colunas para "derreter", retorna um DF vazio com a estrutura esperada
        return pd.DataFrame(columns=['timestamp', 'serial_number', 'potencia_W', 'tipo', 'potencia_kW'])
    # --- FIM DA CORREÇÃO 2 ---
    
    # Transforma de "wide" (colunas por serial) para "long" (para o Plotly Express)
    df_long = df_dense_min.melt(
        id_vars='timestamp', 
        value_vars=cols_to_melt, # Agora usa a lista segura
        var_name='serial_number', 
        value_name='potencia_W'
    )
    df_long['tipo'] = 'Carregador'
    df_long['potencia_kW'] = df_long['potencia_W'] / 1000.0
    
    return df_long


# --- FUNÇÃO PRINCIPAL QUE CONSTRÓI O DASHBOARD (COM CORREÇÕES NA LÓGICA DE DADOS) ---
def build_dashboard():
    # Inicializa DataFrames auxiliares vazios para evitar erro
    df_site_raw = pd.DataFrame()
    df_profile_raw = pd.DataFrame()
    df_power_filtered = pd.DataFrame()
    # --- Carregar e Processar os Dados (antes de qualquer uso de min_date/max_date) ---
    log_path = "external_data/logs_combinados.log"
    chargers, status_events, control_events, all_times, site_power_events = parse_log(log_path)
    # Montar DataFrames para compatibilidade com o restante do dashboard
    all_power = []
    all_status = []
    for cp_id, events in chargers.items():
        for e in events:
            all_power.append({"timestamp": e["timestamp"], "serial_number": cp_id, "potencia_W": e["power"]})
    for cp_id, events in status_events.items():
        for e in events:
            all_status.append({"timestamp": e["timestamp"], "serial_number": cp_id, "status": e["status"]})
    df_power_raw = pd.DataFrame(all_power)
    df_status_raw = pd.DataFrame(all_status)
    # Adiciona colunas de data/hora
    if not df_power_raw.empty:
        df_power_raw["date"] = df_power_raw["timestamp"].dt.date
        df_power_raw["hour"] = df_power_raw["timestamp"].dt.hour
        df_power_raw["potencia_kW"] = df_power_raw["potencia_W"] / 1000.0
    if not df_status_raw.empty:
        df_status_raw["date"] = df_status_raw["timestamp"].dt.date
        df_status_raw["hour"] = df_status_raw["timestamp"].dt.hour
    # Definir min_date e max_date — calcular o intervalo combinado de todas as fontes conhecidas
    candidate_dates = []
    if not df_power_raw.empty and 'date' in df_power_raw.columns:
        candidate_dates.append(df_power_raw["date"].min())
        candidate_dates.append(df_power_raw["date"].max())
    # all_times vem de parse_log() (lista ordenada de datetimes)
    if 'all_times' in locals() and all_times:
        candidate_dates.append(all_times[0].date())
        candidate_dates.append(all_times[-1].date())
    if candidate_dates:
        min_date = min(candidate_dates)
        max_date = max(candidate_dates)
    else:
        today = datetime.today().date()
        min_date = today
        max_date = today
    # (Removido: uso de show_disconnects antes da definição)

    # --- CSS (Cole o seu CSS aqui, mantido idêntico) ---

    # --- CSS (Cole o seu CSS aqui, mantido idêntico) ---
    st.markdown("""
    <style>
    /* Sidebar cinza claro, texto preto */
    [data-testid="stSidebar"] {
        background-color: #DCDCDC !important;
        color: #222 !important;
    }
    [data-testid="stSidebar"] * {
        color: #222 !important;
    }
    /* Inputs, selects, botões: fundo branco, texto preto */
    input, select, textarea, button, .stButton>button, label[data-testid="stMarkdownContainer"] {
        background-color: #fff !important;
        color: #222 !important;
        border: 1px solid #ccc !important;
    }
    /* Selectbox de hora (filtro) - garantir fundo branco e texto preto */
    .stSelectbox div[data-baseweb="select"] > div {
        background-color: #fff !important;
        color: #222 !important;
    }
    .stSelectbox div[data-baseweb="select"] input {
        background-color: #fff !important;
        color: #222 !important;
    }
    /* Checkboxes transparentes, texto preto */
    .stCheckbox>div>div>input[type="checkbox"] {
        background-color: transparent !important;
    }
    .stCheckbox>label, .stCheckbox label, label[data-testid="stMarkdownContainer"] {
        color: #222 !important;
    }
    /* Área principal branca, texto preto */
    [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
        background-color: #fff !important;
        color: #111 !important;
    }
    [data-testid="stAppViewContainer"] * {
        color: #111 !important;
    }
    /* Estilo do ID do carregador ao lado do título (fundo transparente) */
    .cp-id {
        background: transparent !important;
        color: #111 !important;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
        padding: 2px 6px;
        border: none !important;
        border-radius: 4px;
    }
    /* Terminal de log: tema escuro, altura fixa, sem redimensionar */
    .stTextArea textarea {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace !important;
        font-size: 12px !important;
        line-height: 1.4 !important;
        background-color: #fff !important;
        color: #111 !important;
        border: 1px solid #ccc !important;
        resize: none !important;
    }
    /* Viewer customizado para log com altura fixa e rolagem */
    .log-viewer {
        max-height: 550px;
        overflow: auto;
        background-color: #1e1e1e;
        color: #e8e8e8 !important;
        border: 1px solid #444;
        border-radius: 6px;
        padding: 10px 12px;
        white-space: pre-wrap;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
        font-size: 12px;
        line-height: 1.4;
    }
    .log-viewer .log-time { color: #9cdcfe !important; }
    .log-viewer .log-level.INFO { color: #6A9955 !important; }
    .log-viewer .log-level.ERROR { color: #f44747 !important; }
    .log-viewer .log-level.WARN { color: #dcdcaa !important; }
    .log-viewer .log-level.DEBUG { color: #c586c0 !important; }
    .log-viewer .log-source { color: #569CD6 !important; }
    .log-viewer .log-key { color: #9cdcfe !important; }
    .log-viewer .log-number { color: #ffa500 !important; }
    .log-viewer .log-string { color: #ce9178 !important; }
    
    /* Centralizar o título principal (st.title) */
    .block-container h1 {
        text-align: center !important;
        margin-top: 0.25rem;
        margin-bottom: 0.75rem;
    }
    </style>
    """, unsafe_allow_html=True)

    # ...restante do processamento, gráfico principal, e só depois as caixas de seleção...
    # --- FIM DA CORREÇÃO 1 ---


    # Título principal (centralizado via CSS)
    st.title("Dashboard Interativo de Potência dos Carregadores ⚡")

    # --- Layout da UI (Sidebar) (IDÊNTICO) ---
    try:
        col1, col2, col3 = st.sidebar.columns([1, 2, 1]) 
        with col2:
            st.image("logo-tcharge-600.png", width=150) 
    except FileNotFoundError:
        st.sidebar.warning("Arquivo 'tcharge.png' não encontrado.")
    except Exception as e:
        st.sidebar.error(f"Erro ao carregar logo: {e}")
        
    st.sidebar.header("Filtros de Data e Hora")
    # Determina min_date / max_date com fallback para timestamps do parser quando df_power_raw está vazio
    try:
        if not df_power_raw.empty:
            min_date = df_power_raw["date"].min()
            max_date = df_power_raw["date"].max()
        elif 'all_times' in locals() and all_times:
            min_date = all_times[0].date()
            max_date = all_times[-1].date()
        else:
            min_date = datetime.today().date()
            max_date = min_date
    except Exception:
        # Em caso de qualquer problema, usa hoje como fallback seguro
        min_date = datetime.today().date()
        max_date = min_date

    selected_date = st.sidebar.date_input(
        "Filtrar por Dia",
        value=max_date,
        min_value=min_date,
        max_value=max_date
    )
    st.sidebar.subheader("Filtrar por Hora")
    hour_options = list(range(24))
    col_start, col_end = st.sidebar.columns(2)
    with col_start:
        selected_hour_start = st.selectbox("De:", options=hour_options, index=0)
    with col_end:
        selected_hour_end = st.selectbox("Até:", options=hour_options, index=23)

    st.sidebar.header("Selecione os Carregadores")
    display_options = {
        f"{power/1000:.1f}kW ({serial})": serial 
        for serial, power in CHARGER_MAX_POWER.items()
    }
    selected_serials = []
    for display_name, serial in display_options.items():
        if st.sidebar.checkbox(display_name, value=True, key=serial):
            selected_serials.append(serial)



    # --- Filtrar o DataFrame (COM A LÓGICA DE SEGURANÇA DA CORREÇÃO 1) ---
    
    # 1. Filtra df_power_raw (só se tiver sido processado)
    df_power_filtered = pd.DataFrame()
    if not df_power_raw.empty and 'date' in df_power_raw.columns:
        time_filter = (
            (df_power_raw['date'] == selected_date) &
            (df_power_raw['hour'].between(selected_hour_start, selected_hour_end))
        )
        df_power_filtered = df_power_raw[time_filter & df_power_raw['serial_number'].isin(selected_serials)]
    
    # 2. Filtra df_status_raw (só se tiver sido processado)
    df_status_filtered = pd.DataFrame()
    if not df_status_raw.empty and 'date' in df_status_raw.columns:
        status_time_filter = (
            (df_status_raw['date'] == selected_date) &
            (df_status_raw['hour'].between(selected_hour_start, selected_hour_end))
        )
        df_status_filtered = df_status_raw[status_time_filter & df_status_raw['serial_number'].isin(selected_serials)]
    
    # 3. Filtra df_site_filtered (só se tiver sido processado)
    df_site_filtered = pd.DataFrame()
    if not df_site_raw.empty and 'date' in df_site_raw.columns:
        site_time_filter = (
            (df_site_raw['date'] == selected_date) &
            (df_site_raw['hour'].between(selected_hour_start, selected_hour_end))
        )
        df_site_filtered = df_site_raw[site_time_filter]
    
    # 4. Filtra df_profile_filtered (só se tiver sido processado)
    df_profile_filtered = pd.DataFrame()
    if not df_profile_raw.empty and 'date' in df_profile_raw.columns:
        profile_time_filter = (
            (df_profile_raw['date'] == selected_date) &
            (df_profile_raw['hour'].between(selected_hour_start, selected_hour_end))
        )
        df_profile_filtered = df_profile_raw[profile_time_filter & df_profile_raw['serial_number'].isin(selected_serials)]

    # --- NOVA LÓGICA DE GRÁFICO (analise_log_carregadores.py) ---
    # 1. Preparar dados minuto a minuto, aplicando lógica de status/potência
    if df_power_filtered.empty and df_status_filtered.empty:
        st.warning("Não há dados suficientes para os filtros selecionados.")
        return
    cp_ids = selected_serials
    from datetime import timedelta  # Garante que timedelta está disponível
    # Gera apenas os minutos que realmente existem nos dados filtrados
    timestamps_power = df_power_filtered['timestamp'] if not df_power_filtered.empty else pd.Series(dtype='datetime64[ns]')
    timestamps_status = df_status_filtered['timestamp'] if not df_status_filtered.empty else pd.Series(dtype='datetime64[ns]')
    if not timestamps_power.empty or not timestamps_status.empty:
        min_time = min(timestamps_power.min(), timestamps_status.min()) if not timestamps_power.empty and not timestamps_status.empty else (timestamps_power.min() if not timestamps_power.empty else timestamps_status.min())
        max_time = max(timestamps_power.max(), timestamps_status.max()) if not timestamps_power.empty and not timestamps_status.empty else (timestamps_power.max() if not timestamps_power.empty else timestamps_status.max())
        all_minutes = pd.date_range(start=min_time.floor('min'), end=max_time.floor('min'), freq='T')
    else:
        all_minutes = pd.Series(dtype='datetime64[ns]')
    # Junta todos os timestamps presentes nos dados filtrados
    times_day = pd.concat([df_power_filtered['timestamp'], df_status_filtered['timestamp']]).sort_values().unique()
    # Usa todos os minutos do intervalo, não só os presentes nos dados
    from datetime import timedelta
    TOLERANCIA_MINUTOS = 2
    status_times = {cp_id: df_status_filtered[df_status_filtered['serial_number'] == cp_id].sort_values('timestamp').to_dict('records') for cp_id in cp_ids}
    power_times = {cp_id: df_power_filtered[df_power_filtered['serial_number'] == cp_id].sort_values('timestamp').to_dict('records') for cp_id in cp_ids}
    data = []
    # Para cada minuto, reinicializa os índices para cada carregador
    for t in all_minutes:
        row = {"timestamp": t}
        total = 0
        for cp_id in cp_ids:
            statuses = status_times.get(cp_id, [])
            powers = power_times.get(cp_id, [])
            # Encontra o último status antes ou igual ao minuto atual
            last_status = "Available"
            for s in statuses:
                if s["timestamp"] <= t:
                    last_status = s["status"]
                else:
                    break
            # Encontra o último valor de potência antes ou igual ao minuto atual
            last_power = 0
            last_power_time = None
            for p in powers:
                if p["timestamp"] <= t:
                    last_power = p["potencia_W"]
                    last_power_time = p["timestamp"]
                else:
                    break
            # Lógica: Se status não for 'Charging', mas houve evento de potência nos últimos X minutos, considera que está carregando
            carregando = False
            if last_status == 'Charging':
                carregando = True
            elif last_power_time is not None and (t - last_power_time) <= timedelta(minutes=TOLERANCIA_MINUTOS):
                carregando = True
            if not carregando:
                last_power = 0
            row[cp_id] = last_power
            total += last_power
        row['total_power'] = total
        data.append(row)
    df = pd.DataFrame(data)
    # Agrupa por minuto
    if not df.empty:
        df['minute'] = df['timestamp'].dt.floor('min')
        agg_dict = {cp_id: 'mean' for cp_id in cp_ids}
        agg_dict['total_power'] = 'mean'
        df_min = df.groupby('minute').agg(agg_dict).reset_index().rename(columns={'minute': 'timestamp'})
    else:
        df_min = pd.DataFrame()
    # Gráfico dos carregadores individuais
    st.markdown("<h2 style='text-align: center;'>Potência ao Longo do Tempo</h2>", unsafe_allow_html=True)
    if df_min.empty or len(df_min) < 2:
        st.warning("Não há dados suficientes para os filtros selecionados.")
        return
    fig = go.Figure()
    custom_names = {
        "0000324070000979": "0000324070000979 - 30kW (A)",
        "0000324070001003": "0000324070001003 - 30kW (B)",
        "125020001113": "125020001113 - 7.5kW (A)",
        "125020001122": "125020001122 - 7.5kW (B)",
        "125020001148": "125020001148 - 7.5kW (C)",
        "125020001128": "125020001128 - 7.5kW (D)"
    }
    for cp_id in cp_ids:
        nome_legenda = custom_names.get(cp_id, str(cp_id))
        fig.add_trace(go.Scatter(
            x=df_min['timestamp'],
            y=df_min[cp_id],
            mode='lines+markers',
            name=nome_legenda,
            hovertemplate=f"Carregador: {nome_legenda}<br>Horário: %{{x}}<br>Potência: %{{y}} W"
        ))
    # Gráfico da soma total dos carregadores
    fig.add_trace(go.Scatter(
        x=df_min['timestamp'],
        y=df_min['total_power'],
        mode='lines',
        name='Potência Ativa Total Carregadores',
        line=dict(color='black', width=3, dash='dash'),
        hovertemplate='Total Carregadores<br>Horário: %{x}<br>Potência: %{y} W'
    ))
    # Adiciona traço do consumo total do site
    if site_power_events:
        df_site_power = pd.DataFrame(site_power_events)
        # Adiciona colunas de data/hora para filtrar igual aos outros
        df_site_power['date'] = df_site_power['timestamp'].dt.date
        df_site_power['hour'] = df_site_power['timestamp'].dt.hour
        site_time_filter = (
            (df_site_power['date'] == selected_date) &
            (df_site_power['hour'].between(selected_hour_start, selected_hour_end))
        )
        df_site_power_filtered = df_site_power[site_time_filter]
        # Agrupa por minuto (média por minuto)
        if not df_site_power_filtered.empty:
            df_site_power_filtered['minute'] = df_site_power_filtered['timestamp'].dt.floor('min')
            df_site_power_min = df_site_power_filtered.groupby('minute')['power'].mean().reset_index()
            fig.add_trace(go.Scatter(
                x=df_site_power_min['minute'],
                y=df_site_power_min['power'],
                mode='lines',
                name='Consumo Total Site',
                line=dict(color='blue', width=2, dash='dot'),
                hovertemplate='Consumo Total Site<br>Horário: %{x}<br>Potência: %{y} W'
            ))
    # Linha de controle de demanda
    fig.add_shape(
        type='line',
        x0=df_min['timestamp'].min(),
        y0=60000,
        x1=df_min['timestamp'].max(),
        y1=60000,
        line=dict(color='red', width=2, dash='dot'),
    )
    fig.add_trace(go.Scatter(
        x=[df_min['timestamp'].min(), df_min['timestamp'].max()],
        y=[60000, 60000],
        mode='lines',
        name='Limite Controle de Demanda',
        line=dict(color='red', width=2, dash='dot'),
        showlegend=True
    ))
    fig.update_layout(
        plot_bgcolor="white",
        paper_bgcolor="white",
        font_color="black",
        legend_title_font_color="black",
        legend_font_color="black",
        xaxis=dict(showgrid=True, gridcolor="lightgray"),
        yaxis=dict(showgrid=True, gridcolor="lightgray"),
        hovermode="x unified"
    )
    st.plotly_chart(fig, use_container_width=True, theme=None)
    # --- Caixas de seleção abaixo do gráfico ---
    st.markdown("<br>", unsafe_allow_html=True)
    col_opts = st.columns([1,1,1,2])
    show_raw = col_opts[0].checkbox("Mostrar dados brutos extraídos do log (tabela)", value=False, key="show_raw_checkbox_tabela_final")
    show_disconnects = col_opts[1].checkbox("Mostrar Quant. de Desconexão", value=False, key="show_disconnects_checkbox_final")
    show_soc_power = col_opts[2].checkbox("Mostrar SoC e Potência Entregue", value=False, key="show_soc_power_checkbox_final")
    show_raw_log = col_opts[3].checkbox("Mostrar log bruto por carregador (terminal)", value=False, key="show_raw_log_checkbox_terminal_final")

    if show_raw:
        st.subheader("Dados Extraídos (Processados para Plotagem)")
        if not df_min.empty:
            st.dataframe(df_min)
        else:
            st.info("Nenhum dado encontrado para os filtros selecionados.")

    if show_disconnects:
        disconnects = get_disconnects(log_path)
        all_known = set(list(CHARGER_MAX_POWER.keys())) | set(disconnects.keys())
        rows = []
        for cp_id in sorted(all_known):
            info = disconnects.get(cp_id, {'local': {}, 'external': {}})
            local_days = info.get('local', {})
            ext_days = info.get('external', {})
            local_count_day = local_days.get(selected_date, 0)
            ext_count_day = ext_days.get(selected_date, 0)
            total_local = sum(local_days.values()) if local_days else 0
            total_external = sum(ext_days.values()) if ext_days else 0
            days_local_list = ", ".join(sorted(d.isoformat() for d in local_days.keys())) if local_days else "-"
            days_ext_list = ", ".join(sorted(d.isoformat() for d in ext_days.keys())) if ext_days else "-"
            rows.append({
                "Carregador": cp_id,
                "Desconexões Locais (dia selecionado)": local_count_day,
                "Desconexões Externas (dia selecionado)": ext_count_day,
                "Total desconexões locais (log)": total_local,
                "Total desconexões externas (log)": total_external,
                "Dias com desconexões (local)": days_local_list,
                "Dias com desconexões (externo)": days_ext_list
            })
        df_disc = pd.DataFrame(rows)
        st.markdown("## Quantidade de Desconexões por Carregador (Local e Externa)")
        st.dataframe(df_disc)
        missing_today = [r["Carregador"] for r in rows if (r["Desconexões Locais (dia selecionado)"] == 0 and r["Desconexões Externas (dia selecionado)"] == 0)]
        if len(missing_today) == len(rows):
            st.info("Nenhuma desconexão registrada para os carregadores no dia selecionado. Tente selecionar uma data anterior/igual ao dia dos logs (ex.: 2025-11-05).")

    if show_soc_power:
        # Extrai SoC e potência entregue do log usando o timestamp do início da linha (horário local do log)
        soc_re = re.compile(r'\[FROM CHARGER ([^]]+)\]:.*MeterValues.*?"sampledValue":\[(.*?)\]\}\]\}\]', re.DOTALL)
        sampled_item_re = re.compile(r'\{(.*?)\}')
        soc_rows = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                m = soc_re.search(line)
                if m:
                    cp_id = m.group(1)
                    sampled = m.group(2)
                    soc = None
                    power = None
                    for item in sampled_item_re.findall(sampled):
                        measurand_match = re.search(r'"measurand"\s*:\s*"([^"]+)"', item)
                        value_match = re.search(r'"value"\s*:\s*"([\d.]+)"', item)
                        if measurand_match and value_match:
                            measurand = measurand_match.group(1)
                            value = float(value_match.group(1))
                            if measurand == "SoC":
                                soc = value
                            if measurand == "Power.Active.Import":
                                power = value
                    try:
                        ts_str = line.split(" - ")[0]
                        log_ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    except Exception:
                        continue
                    if log_ts.date() == selected_date and (soc is not None or power is not None):
                        soc_rows.append({
                            "Carregador": cp_id,
                            "Horário": log_ts,
                            "SoC (%)": soc if soc is not None else "-",
                            "Potência Entregue (W)": power if power is not None else "-"
                        })
        if soc_rows:
            df_soc = pd.DataFrame(soc_rows)
            st.markdown("## SoC (Bateria) e Potência Entregue por Carregador")
            fig_soc = go.Figure()
            max_gap_minutes = 10
            color_map = {"SoC": "#1f77b4", "Potência": "#d62728"}
            for cp_id in df_soc["Carregador"].unique():
                df_cp = df_soc[df_soc["Carregador"] == cp_id].sort_values("Horário")
                soc_vals = [v if v != "-" else None for v in df_cp["SoC (%)"]]
                soc_times = df_cp["Horário"].tolist()
                pot_vals = [v if v != "-" else None for v in df_cp["Potência Entregue (W)"]]
                pot_times = df_cp["Horário"].tolist()
                def split_sessions(times, vals):
                    if not times:
                        return []
                    sessions = []
                    session_x = [times[0]]
                    session_y = [vals[0]]
                    for i in range(1, len(times)):
                        gap = (times[i] - times[i-1]).total_seconds() / 60.0
                        if gap > max_gap_minutes or vals[i] is None:
                            if len(session_x) > 1:
                                sessions.append((session_x, session_y))
                            session_x = []
                            session_y = []
                        session_x.append(times[i])
                        session_y.append(vals[i])
                    if len(session_x) > 1:
                        sessions.append((session_x, session_y))
                    return sessions
                first_soc = True
                for sess_x, sess_y in split_sessions(soc_times, soc_vals):
                    fig_soc.add_trace(go.Scatter(x=sess_x, y=sess_y, mode="lines+markers",
                                                 name=f"SoC (%) - {cp_id}" if first_soc else None,
                                                 yaxis="y1", line=dict(color=color_map["SoC"])) )
                    first_soc = False
                first_pot = True
                for sess_x, sess_y in split_sessions(pot_times, pot_vals):
                    fig_soc.add_trace(go.Scatter(x=sess_x, y=sess_y, mode="lines+markers",
                                                 name=f"Potência (W) - {cp_id}" if first_pot else None,
                                                 yaxis="y2", line=dict(color=color_map["Potência"], dash="dot")))
                    first_pot = False
            fig_soc.update_layout(xaxis_title="Horário",
                                   yaxis=dict(title="SoC (%)", side="left", showgrid=True, gridcolor="lightgray"),
                                   yaxis2=dict(title="Potência Entregue (W)", overlaying="y", side="right", showgrid=False),
                                   legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                                   plot_bgcolor="white", paper_bgcolor="white", font_color="black")
            st.plotly_chart(fig_soc, use_container_width=True, theme=None)
        else:
            st.info("Nenhum dado de SoC ou potência entregue encontrado para o dia selecionado.")

    if show_raw_log:
        # Monta lista de carregadores a partir do parse_log (mais eficiente que reler todo o arquivo)
        carregadores = set()
        try:
            # 'chargers' vem de parse_log acima e contém CP IDs conhecidos
            carregadores.update(list(chargers.keys()))
        except Exception:
            pass
        # Garante que os serials conhecidos também sejam mostrados
        carregadores.update(list(CHARGER_MAX_POWER.keys()))
        carregadores = sorted(list(carregadores))

        if not carregadores:
            st.info("Nenhum carregador encontrado no log para exibição bruta.")
        else:
            selected_cp = st.selectbox("Selecione o carregador para log bruto", carregadores, key="selectbox_log_cp_unique")
            if selected_cp:
                # Limite de linhas a renderizar (para evitar travamento do navegador)
                # Tornamos isso configurável no sidebar para não precisar rebuildar o container Docker
                try:
                    MAX_LINES_RENDER = int(st.sidebar.number_input("Máx. linhas a renderizar no terminal de log", min_value=50, max_value=20000, value=2000, step=50, key="max_lines_render"))
                except Exception:
                    MAX_LINES_RENDER = 2000
                @st.cache_data
                def get_log_lines_for_cp(path, cp_id, max_lines):
                    dq = deque(maxlen=max_lines)
                    total = 0
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            if cp_id in line:
                                total += 1
                                dq.append(line.rstrip())
                    return list(dq), total

                log_lines, total_matches = get_log_lines_for_cp(log_path, selected_cp, MAX_LINES_RENDER)
                st.markdown(f"### Log bruto do carregador: <span class='cp-id'>{selected_cp}</span>", unsafe_allow_html=True)
                if not log_lines:
                    st.info("Nenhuma linha encontrada para este carregador.")
                else:
                    if total_matches > len(log_lines):
                        st.info(f"Mostrando as últimas {len(log_lines)} de {total_matches} linhas correspondentes (truncado). Use o botão de download para obter o log completo para este carregador.")
                    raw = "\n".join(log_lines)
                    # Remove caracteres de controle ASCII (exceto tab/linhas)
                    raw = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", raw)
                    colored = escape(raw)
                    # timestamps
                    colored = re.sub(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', r"<span class='log-time'>\1</span>", colored)
                    # níveis
                    colored = re.sub(r'\b(INFO|ERROR|WARN|DEBUG)\b', r"<span class='log-level \1'>\1</span>", colored)
                    # fontes (ex.: [FROM CHARGER ...], [TO EXTERNAL SERVER ...], [STATE UPDATE ...])
                    colored = re.sub(r'\[(?:FROM|TO|STATE UPDATE|CONTROL|GATEWAY CONSUME) [^\]]+\]', lambda m: f"<span class='log-source'>{m.group(0)}</span>", colored)
                    # chaves JSON
                    colored = re.sub(r'&quot;([^&"]+)&quot;(?=\s*:)', r'&quot;<span class="log-key">\1</span>&quot;', colored)
                    # strings JSON (após os dois pontos)
                    colored = re.sub(r'(?<=:\s)&quot;([^&]*)&quot;', r'&quot;<span class="log-string">\1</span>&quot;', colored)
                    # números após ':' ou '=' seguidos de 'W' (destacar apenas valores em Watts)
                    colored = re.sub(r'(?<=[:=]\s)(-?\d+(?:\.\d+)?)(?=\s*W\b)', r"<span class='log-number'>\1</span>", colored)
                    st.markdown(f"<div class='log-viewer'>{colored}</div>", unsafe_allow_html=True)
                    # Oferece download do conteúdo completo para este carregador (caso queira todo o histórico)
                    if total_matches > 0:
                        # botão de download com todas as linhas correspondentes (poderá ser custoso se for muito grande)
                        @st.cache_data
                        def get_full_log_for_cp(path, cp_id):
                            lines = []
                            with open(path, encoding="utf-8") as f:
                                for line in f:
                                    if cp_id in line:
                                        lines.append(line.rstrip())
                            return "\n".join(lines)

                        full_text = get_full_log_for_cp(log_path, selected_cp)
                        st.download_button(label="Download do log completo para este carregador", data=full_text, file_name=f"log_{selected_cp}.log", mime="text/plain")


# --- LÓGICA DE EXECUÇÃO PRINCIPAL (O "PORTÃO") (IDÊNTICA) ---
# 1. Verifica a senha
if check_password():
    build_dashboard()