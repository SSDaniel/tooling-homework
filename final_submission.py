import streamlit as st
import pandas as pd
import plotly.express as px
import re 
from datetime import datetime, timedelta # timedelta é usado, mas pd.Timedelta é usado na comparação
import numpy as np 

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


# --- Função de Parsing do Log (IDÊNTICA AO SEU CÓDIGO) ---
@st.cache_data 
def load_and_parse_log(log_file, known_serials):
    """
    Lê o arquivo de log e extrai 4 tipos de dados:
    1. Potência (de [STATE UPDATE])
    2. Status (de [FROM CHARGER]...StatusNotification)
    3. Potência do Site (de [CONTROL]...Consumo Total Site)
    4. Eventos SetProfile (para as estrelas)
    """
    # 1. Padrão para potência do carregador
    charger_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - INFO - \[STATE UPDATE (.*?)\]: Potência atual: (.*?)W"
    )
    # 2. Padrão para consumo total do site
    site_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - INFO - \[CONTROL\].*?Consumo Total Site: (.*?)W"
    )
    # 3. Padrão para eventos "Setchargerprofile"
    setprofile_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?\[.*?(\d{12,}).*?\].*?Setchargerprofile", re.IGNORECASE
    )
    # 4. NOVO PADRÃO: Eventos de Status
    statusnotif_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*\[FROM CHARGER ([^]]+)\]:.*StatusNotification.*\"status\"\s*:\s*\"([A-Za-z]+)\"", re.IGNORECASE
    )

    power_data = []
    status_data = []
    site_data = []
    profile_data = []
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                # 1. Potência do Carregador
                charger_match = charger_pattern.search(line)
                if charger_match:
                    timestamp, serial, power = charger_match.groups()
                    if serial in known_serials:
                        power_data.append({
                            "timestamp": timestamp,
                            "serial_number": serial,
                            "potencia_W": float(power)
                        })
                    continue 

                # 2. Potência do Site
                site_match = site_pattern.search(line)
                if site_match:
                    timestamp, power = site_match.groups()
                    site_data.append({
                        "timestamp": timestamp,
                        "serial_number": "Consumo Total Site", 
                        "potencia_W": float(power),
                        "tipo": "Site"
                    })
                    continue
                
                # 3. Eventos SetProfile
                profile_match = setprofile_pattern.search(line)
                if profile_match:
                    timestamp, serial = profile_match.groups()
                    if serial in known_serials:
                        profile_data.append({
                            "timestamp": timestamp,
                            "serial_number": serial,
                            "tipo": "SetProfile"
                        })
                    continue
                
                # 4. Eventos de Status
                status_match = statusnotif_re.search(line)
                if status_match:
                    timestamp, serial, status = status_match.groups()
                    if serial in known_serials:
                        status_data.append({
                            "timestamp": timestamp,
                            "serial_number": serial,
                            "status": status
                        })
    
    except FileNotFoundError:
        pass
    except Exception as e:
        st.error(f"Erro ao processar o arquivo de log '{log_file}': {e}")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    return pd.DataFrame(power_data), pd.DataFrame(status_data), pd.DataFrame(site_data), pd.DataFrame(profile_data)


# --- FUNÇÃO DE VERIFICAÇÃO DE SENHA (IDÊNTICA) ---
def check_password():
    """Retorna True se o usuário digitar a senha correta, False caso contrário."""
    
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
    df_power_csv, df_status_csv, df_site_csv, df_profile_csv = load_and_parse_log("logs_combinados_cronologicamente.log", KNOWN_SERIALS)
    df_power_log, df_status_log, df_site_log, df_profile_log = load_and_parse_log("external_data/logs_combinados_cronologicamente.log", KNOWN_SERIALS)

    if df_power_csv.empty and df_site_csv.empty and df_power_log.empty and df_site_log.empty:
        st.error("Erro: Não foi possível encontrar 'logs_combinados_cronologicamente' ou 'logs_combinados_cronologicamente'.")
        st.stop()
        
    df_power_raw = pd.concat([df_power_csv, df_power_log])
    df_status_raw = pd.concat([df_status_csv, df_status_log])
    df_site_raw = pd.concat([df_site_csv, df_site_log])
    df_profile_raw = pd.concat([df_profile_csv, df_profile_log])

    if df_power_raw.empty and df_site_raw.empty:
        st.warning("O arquivo de log foi lido, mas nenhum dado de potência foi encontrado.")
        st.stop()

    # --- CORREÇÃO 1: Processamento de datas e adição de colunas movidos para aqui ---
    all_dfs = []
    # Converte timestamps e ADICIONA COLUNAS DE FILTRO (date, hour) imediatamente
    for df in [df_power_raw, df_status_raw, df_site_raw, df_profile_raw]:
        if not df.empty:
            df['timestamp'] = df['timestamp'].str.replace(',', '.', regex=False)
            try:
                df['timestamp'] = pd.to_datetime(df['timestamp'], format='mixed')
                # Adiciona as colunas 'date' e 'hour' ao DataFrame original
                df['date'] = df['timestamp'].dt.date
                df['hour'] = df['timestamp'].dt.hour
                all_dfs.append(df) # Adiciona à lista SÓ se a conversão funcionou
            except ValueError as e:
                st.error(f"Erro ao converter datas mesmo após a correção: {e}")
                st.stop()
    
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

    # 5. Pega TODOS os timestamps únicos dos eventos de status e potência FILTRADOS (IDÊNTICO)
    all_timestamps_filtered = sorted(
        pd.concat([df_power_filtered['timestamp'], df_status_filtered['timestamp']]).unique()
    )
    
    # 6. CHAMA A NOVA FUNÇÃO DE PROCESSAMENTO (IDÊNTICO)
    if not all_timestamps_filtered:
        df_chargers_processed = pd.DataFrame()
    else:
        df_chargers_processed = process_data_no_ramps(
            df_power_filtered, 
            df_status_filtered, 
            all_timestamps_filtered, 
            selected_serials
        )

    # 7. Cria o dataframe final para plotagem (IDÊNTICO)
    df_plot = pd.concat([df_chargers_processed, df_site_filtered])

    # 8. Processa os eventos "SetProfile" (Estrelas) (IDÊNTICO)
    df_events_with_power = pd.DataFrame()
    if not df_profile_filtered.empty and not df_chargers_processed.empty:
        df_events_with_power = pd.merge_asof(
            df_profile_filtered.sort_values('timestamp'),
            df_chargers_processed.sort_values('timestamp').dropna(subset=['potencia_kW']),
            on='timestamp',
            by='serial_number',
            direction='nearest' 
        )

    # --- Gráfico Interativo (Usando Plotly) (IDÊNTICO) ---
    st.markdown("<h2 style='text-align: center;'>Potência ao Longo do Tempo</h2>", unsafe_allow_html=True)

    if df_plot.empty or len(df_plot) < 2:
        st.warning("Não há dados suficientes para os filtros selecionados.")
    else:
        # 1. Criar o gráfico de LINHAS principal
        fig = px.line(
            df_plot,
            x="timestamp",
            y="potencia_kW",
            color="serial_number",
            template="plotly_white", 
            labels={ 
                "timestamp": "Data e Hora",
                "potencia_kW": "Potência (kW)",
                "serial_number": "Série / Medição"
            },
            hover_data={ 
                "potencia_kW": ":.2f", 
                "timestamp": "|%Y-%m-%d %H:%M" 
            }
        )
        
        # 2. Adicionar a camada de ESTRELAS
        if not df_events_with_power.empty:
            fig_events = px.scatter(
                df_events_with_power,
                x="timestamp",
                y="potencia_kW", 
                color="serial_number"
            )
            
            fig_events.update_traces(
                marker=dict(symbol='star', size=15), 
                showlegend=False 
            )
            
            for trace in fig_events.data:
                fig.add_trace(trace)

        # 3. Adicionar linha vermelha fixa
        fig.add_hline(
            y=60, 
            line_dash="dash", 
            line_color="red", 
            annotation_text="Limite 60 kW", 
            annotation_position="bottom right"
        )

        # 4. Aplicar o layout (fundo branco, etc.)
        fig.update_layout(
            plot_bgcolor="white",    
            paper_bgcolor="white",   
            font_color="black",      
            legend_title_font_color="black",
            legend_font_color="black",
            xaxis=dict(showgrid=True, gridcolor="lightgray"),
            yaxis=dict(showgrid=True, gridcolor="lightgray")
        )
        
        fig.update_traces(mode='lines') 

        # 5. Renderizar o gráfico
        st.plotly_chart(fig, use_container_width=True, theme=None) 


    # --- Mostrar Dados Brutos (Opcional) (IDÊNTICO) ---
    if st.checkbox("Mostrar dados brutos extraídos do log"):
        st.subheader("Dados Extraídos (Processados para Plotagem)")
        st.dataframe(df_plot)
        if not df_events_with_power.empty:
            st.subheader("Dados Extraídos (Eventos - Estrelas)")
            st.dataframe(df_events_with_power)


# --- LÓGICA DE EXECUÇÃO PRINCIPAL (O "PORTÃO") (IDÊNTICA) ---
# 1. Verifica a senha
if check_password():
    # 2. Se a senha for correta, constrói o dashboard
    build_dashboard()