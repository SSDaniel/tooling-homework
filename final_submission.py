import streamlit as st
import pandas as pd
import plotly.express as px
import re 
from datetime import datetime
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


# --- Função de Parsing do Log (Definida no escopo global) ---
@st.cache_data 
def load_and_parse_log(log_file, known_serials):
    """
    Lê o arquivo de log linha por linha e extrai os dados de potência
    e os eventos 'Setchargerprofile'.
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

    data = []
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                charger_match = charger_pattern.search(line)
                if charger_match:
                    timestamp, serial, power = charger_match.groups()
                    if serial in known_serials:
                        data.append({
                            "timestamp": timestamp,
                            "serial_number": serial,
                            "potencia_W": float(power),
                            "tipo": "Carregador"
                        })
                    continue 

                site_match = site_pattern.search(line)
                if site_match:
                    timestamp, power = site_match.groups()
                    data.append({
                        "timestamp": timestamp,
                        "serial_number": "Consumo Total Site", 
                        "potencia_W": float(power),
                        "tipo": "Site"
                    })
                    continue
                
                profile_match = setprofile_pattern.search(line)
                if profile_match:
                    timestamp, serial = profile_match.groups()
                    if serial in known_serials:
                        data.append({
                            "timestamp": timestamp,
                            "serial_number": serial,
                            "potencia_W": np.nan, 
                            "tipo": "SetProfile" 
                        })

    except FileNotFoundError:
        pass
    except Exception as e:
        st.error(f"Erro ao processar o arquivo de log '{log_file}': {e}")
        return pd.DataFrame()

    return pd.DataFrame(data)


# --- FUNÇÃO DE VERIFICAÇÃO DE SENHA ---
def check_password():
    """Retorna True se o usuário digitar a senha correta, False caso contrário."""
    
    # Tenta obter a senha dos "Secrets" do Streamlit
    try:
        correct_password = st.secrets["passwords"]["admin_password"]
    except KeyError:
        st.error("Erro: Senha de administrador não configurada nos 'Secrets' do Streamlit.")
        st.stop() # Interrompe a execução

    # Pedir a senha ao usuário
    password_attempt = st.text_input("Digite a senha para acessar o dashboard:", type="password")

    # Se a senha ainda não foi digitada, para a execução
    if not password_attempt:
        st.info("Por favor, digite a senha para continuar.")
        st.stop()

    # Verifica se a senha está correta
    if password_attempt == correct_password:
        return True
    else:
        st.error("Senha incorreta. Tente novamente.")
        return False

# --- FUNÇÃO PRINCIPAL QUE CONSTRÓI O DASHBOARD ---
# (Todo o seu código original foi movido para cá)
def build_dashboard():

    # --- CSS: Fundo (Branco), Sidebar (Azul Claro) e Texto (Preto) ---
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


    # --- Carregar e Processar os Dados ---
    df_csv = load_and_parse_log("external_data/log_2025-10-29_11-43-52.log", KNOWN_SERIALS)

    if df_csv.empty:
        df_log = load_and_parse_log("external_data/log_2025-10-29_11-43-52.log", KNOWN_SERIALS)
        df = df_log
        if df_log.empty:
            st.error("Erro: Não foi possível encontrar 'log_2025-10-29_11-43-52.csv' ou 'log_2025-10-29_11-43-52.log'.")
            st.stop()
    else:
        df = df_csv

    # Processamento de dados (com correção do timestamp)
    if not df.empty:
        # 1. Substitui a vírgula (,) por um ponto (.) nos timestamps
        df['timestamp'] = df['timestamp'].str.replace(',', '.', regex=False)
        
        try:
            df['timestamp'] = pd.to_datetime(df['timestamp'], format='mixed')
        except ValueError as e:
            st.error(f"Erro ao converter datas mesmo após a correção: {e}")
            st.stop()

        df['potencia_kW'] = df['potencia_W'] / 1000.0
        df = df.sort_values(by="timestamp")
        
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour
        
        min_date = df['date'].min()
        max_date = df['date'].max()
        
        df_events_raw = df[df['tipo'] == 'SetProfile'].copy()
        df_data = df[df['tipo'] != 'SetProfile'].copy() 
        
    else:
        st.warning("O arquivo de log foi lido, mas nenhum dado de potência foi encontrado.")
        st.stop()


    # --- Layout da UI (Sidebar) ---
    try:
        col1, col2, col3 = st.sidebar.columns([1, 2, 1]) 
        with col2:
            st.image("logo-tcharge-600.png", width=150) 
    except FileNotFoundError:
        st.sidebar.warning("Arquivo 'tcharge.png' não encontrado.")
    except Exception as e:
        st.sidebar.error(f"Erro ao carregar logo: {e}")

    # Filtros de Data e Hora
    st.sidebar.header("Filtros de Data e Hora")
    selected_date = st.sidebar.date_input(
        "Filtrar por Dia",
        value=max_date, 
        min_value=min_date,
        max_value=max_date
    )

    # --- AJUSTE: Trocado st.select_slider por dois st.selectbox ---
    st.sidebar.subheader("Filtrar por Hora")
    hour_options = list(range(24)) # Lista de 0 a 23

    col_start, col_end = st.sidebar.columns(2)
    with col_start:
        selected_hour_start = st.selectbox(
            "De:", 
            options=hour_options, 
            index=0 # Padrão 00:00
        )
    with col_end:
        selected_hour_end = st.selectbox(
            "Até:", 
            options=hour_options, 
            index=23 # Padrão 23:00
        )
    # --- FIM DO AJUSTE ---

    # Filtro de Carregadores
    st.sidebar.header("Selecione os Carregadores")
    display_options = {
        f"{power/1000:.1f}kW ({serial})": serial 
        for serial, power in CHARGER_MAX_POWER.items()
    }
    selected_serials = []
    for display_name, serial in display_options.items():
        if st.sidebar.checkbox(display_name, value=True, key=serial):
            selected_serials.append(serial)


    # --- Filtrar o DataFrame (Multi-etapa) ---
    df_data_filtered = df_data[
        (df_data['date'] == selected_date) &
        (df_data['hour'].between(selected_hour_start, selected_hour_end)) 
    ]
    df_events_filtered = df_events_raw[
        (df_events_raw['date'] == selected_date) &
        (df_events_raw['hour'].between(selected_hour_start, selected_hour_end)) 
    ]

    df_site = df_data_filtered[df_data_filtered['tipo'] == 'Site']
    df_chargers = df_data_filtered[df_data_filtered['serial_number'].isin(selected_serials)]
    df_plot = pd.concat([df_site, df_chargers])

    df_events_with_power = pd.DataFrame()
    if not df_events_filtered.empty and not df_chargers.empty:
        df_events_filtered = df_events_filtered[df_events_filtered['serial_number'].isin(selected_serials)]
        
        df_events_with_power = pd.merge_asof(
            df_events_filtered.sort_values('timestamp'),
            df_chargers.sort_values('timestamp').dropna(subset=['potencia_kW']),
            on='timestamp',
            by='serial_number',
            direction='nearest' 
        )

    # --- Gráfico Interativo (Usando Plotly) ---
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


    # --- Mostrar Dados Brutos (Opcional) ---
    if st.checkbox("Mostrar dados brutos extraídos do log"):
        st.subheader("Dados Extraídos (Linhas)")
        st.dataframe(df_plot)
        if not df_events_with_power.empty:
            st.subheader("Dados Extraídos (Eventos - Estrelas)")
            st.dataframe(df_events_with_power)


# --- LÓGICA DE EXECUÇÃO PRINCIPAL (O "PORTÃO") ---
# 1. Verifica a senha
if check_password():
    # 2. Se a senha for correta, constrói o dashboard
    build_dashboard()