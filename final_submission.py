import streamlit as st
import pandas as pd
import plotly.graph_objs as go
import plotly.express as px
import re
from datetime import datetime, timedelta
import numpy as np
import os

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
    disconnect_re = re.compile(r"\[Local Server\] Cliente '([^']+)' desconectado e removido\." )
    disconnects = {}
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            m = disconnect_re.search(line)
            if m:
                cp_id = m.group(1)
                try:
                    ts_str = line.split(" - ")[0]
                    timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
                    day = timestamp.date()
                    if cp_id not in disconnects:
                        disconnects[cp_id] = {}
                    disconnects[cp_id][day] = disconnects[cp_id].get(day, 0) + 1
                except Exception:
                    continue
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
    # (Removido: uso de show_disconnects antes da definição)

    # --- CSS (Cole o seu CSS aqui, mantido idêntico) ---
    st.markdown("""
    <style>
            
    input, select, textarea {
        background-color: white !important;
        color: black !important;
    }

    /* Deixar a barra superior branca */
    [data-testid="stHeader"] {
        background-color: white !important;
        color: black !important;
    }

    /* Esconder a sombra escura sob a barra */
    [data-testid="stHeader"]::before {
        background: none !important;
    }

    /* Ícones e botões da barra (menu, etc.) em preto */
    [data-testid="stHeader"] svg, 
    [data-testid="stHeader"] button {
        color: black !important;
        fill: black !important;
    }
                        

    [data-testid="stHeader"] {
        box-shadow: none !important;
        border-bottom: 1px solid #ddd !important;
    }

            
    /* Cor de fundo principal (branca) */
    [data-testid="stAppViewContainer"] {
        background-color: #FFFFFF;
    }
    /* Cor de fundo da sidebar (azul claro) */
    [data-testid="stSidebar"] {
        background-color: #DCDCDC ;
    }

    /* Força todo o texto para PRETO */
    [data-testid="stAppViewContainer"] *, [data-testid="stSidebar"] * {
        color: black !important;
    }
    [data-testid="stCheckbox"] label {
        color: black !important;
    }

    /* Centralizar o Título Principal (h1) */
    [data-testid="stAppViewContainer"] h1 {
        text-align: center;
    }

    /* --- CORREÇÃO: Forçar widgets (calendário, selectbox) para o tema claro --- */

    /* Caixa principal do Seletor de Data (Calendário) */
    [data-testid="stDateInput"] div[data-baseweb="input"] input {
        background-color: white !important;
        color: black !important;
        border-color: lightgray !important;
    }

    /* Selectbox (mantém branco mesmo com tema escuro) */
    [data-testid="stSelectbox"] div[data-baseweb="select"],
    [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
    [data-testid="stSelectbox"] div[data-baseweb="select"] input {
        background-color: white !important;
        color: black !important;
        border: 1px solid lightgray !important;
    }

    /* O menu suspenso que aparece ao clicar */
    div[data-baseweb="popover"] {
        background-color: white !important;
        color: black !important;
    }
    div[data-baseweb="option"] {
        background-color: white !important;
        color: black !important;
    }
    div[data-baseweb="option"]:hover {
        background-color: #f0f0f0 !important;
    }

    /* Botões (como < >) dentro dos pop-ups */
    div[data-baseweb="popover"] button {
        background-color: lightgray !important;
    }

    </style>
    """, unsafe_allow_html=True) 
    
    st.title("Dashboard Interativo de Potência dos Carregadores ⚡")


    # --- Carregar e Processar os Dados (IDÊNTICO) ---
    # Usar a lógica do analise_log_carregadores.py para parsing
    log_path = "external_data/logs_combinados_cronologicamente1.log"
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
    min_date = df_power_raw["date"].min() if not df_power_raw.empty else datetime.today().date()
    max_date = df_power_raw["date"].max() if not df_power_raw.empty else datetime.today().date()
    # df_site_raw e df_profile_raw mantidos vazios para compatibilidade
    df_site_raw = pd.DataFrame()
    df_profile_raw = pd.DataFrame()
    if df_power_raw.empty:
        st.warning("O arquivo de log foi lido, mas nenhum dado de potência foi encontrado.")
        st.stop()

    # --- CORREÇÃO 1: Processamento de datas e adição de colunas movidos para aqui ---
    all_dfs = []
    # Converte timestamps e ADICIONA COLUNAS DE FILTRO (date, hour) imediatamente
    for df in [df_power_raw, df_status_raw, df_site_raw, df_profile_raw]:
        if not df.empty:
            # Se a coluna já for datetime, não faz replace nem converte
            if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
                df['timestamp'] = df['timestamp'].str.replace(',', '.', regex=False)
                try:
                    df['timestamp'] = pd.to_datetime(df['timestamp'], format='mixed')
                except ValueError as e:
                    st.error(f"Erro ao converter datas mesmo após a correção: {e}")
                    st.stop()
            # Adiciona as colunas 'date' e 'hour' ao DataFrame original
            df['date'] = df['timestamp'].dt.date
            df['hour'] = df['timestamp'].dt.hour
            all_dfs.append(df) # Adiciona à lista SÓ se a conversão funcionou
    
    if not all_dfs:
        st.warning("Nenhum dado com timestamp válido foi encontrado nos logs.")
        st.stop()
        
    # Pega o range de datas de TODOS os eventos combinados
    df_all_times = pd.concat([df['timestamp'] for df in all_dfs])
    min_date = df_all_times.min().date()
    max_date = df_all_times.max().date()
    
    # Adiciona potencia_kW ao df_site_raw (mantido)
    if not df_site_raw.empty:
        df_site_raw['potencia_kW'] = df_site_raw['potencia_W'] / 1000.0
    # --- FIM DA CORREÇÃO 1 ---


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
    # Caixas de seleção abaixo do gráfico (declaradas após todas variáveis)
    st.markdown("<br>", unsafe_allow_html=True)
    col_opts = st.columns([1,1])
    show_raw = col_opts[0].checkbox("Mostrar dados brutos extraídos do log", value=False, key="show_raw_checkbox")
    show_disconnects = col_opts[1].checkbox("Mostrar Quant. de Desconexões", value=False, key="show_disconnects_checkbox")
    if show_raw:
        st.subheader("Dados Extraídos (Processados para Plotagem)")
        st.dataframe(df_min)
    if show_disconnects:
        disconnects = get_disconnects("external_data/logs_combinados_cronologicamente1.log")
        rows = []
        for cp_id, days in disconnects.items():
            count = days.get(selected_date, 0)
            rows.append({"Carregador": cp_id, "Desconexões": count})
        df_disc = pd.DataFrame(rows)
        st.markdown("## Quantidade de Desconexões por Carregador")
        st.dataframe(df_disc)


# --- LÓGICA DE EXECUÇÃO PRINCIPAL (O "PORTÃO") (IDÊNTICA) ---
# 1. Verifica a senha
if check_password():
    build_dashboard()