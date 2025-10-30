import re
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
import plotly.graph_objs as go
import pandas as pd
import json
import os


power_re = re.compile(r"\[STATE UPDATE ([^]]+)\]: Potência atual: ([\\d.]+)W")
status_re = re.compile(r"\[STATE UPDATE ([^]]+)\]: Status alterado de '([^']+)' para '([^']+)'")
control_re = "[CONTROL] SOBRECARGA!"

def parse_log(log_file):
    chargers = {}
    status_events = {}
    control_events = []
    all_times = set()
    # Regex para StatusNotification no padrão '[FROM CHARGER ...]'
    statusnotif_re = re.compile(r'\[FROM CHARGER ([^]]+)\]:.*StatusNotification.*"status"\s*:\s*"([A-Za-z]+)"')
    # Regex para potência '[STATE UPDATE <id>]: Potência atual: <valor>W'
    power_stateupdate_re = re.compile(r'\[STATE UPDATE ([^]]+)\]: Potência atual: ([\d.]+)W')
    # Regex para identificar controle de demanda aplicado
    control_applied_re = re.compile(r"\[CONTROL\] SOBRECARGA!.*Aplicando balanceamento\.")
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                ts_str = line.split(" - ")[0]
                timestamp = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f")
            except Exception:
                continue
            all_times.add(timestamp)
            # Extrai status do padrão '[FROM CHARGER ...]: ...StatusNotification..."status":"Charging"...'
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
            # Extrai potência do padrão '[STATE UPDATE <id>]: Potência atual: <valor>W'
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
            # Identifica momento de controle de demanda aplicado
            if control_applied_re.search(line):
                control_events.append(timestamp)
    # Filtra apenas IDs que parecem número de série (apenas dígitos ou letras, sem espaços, sem 'EXTERNAL SERVER')
    def is_serial(cp_id):
        return cp_id and not cp_id.startswith("EXTERNAL SERVER") and cp_id.replace(' ', '').isalnum()
    serial_chargers = {cp_id: events for cp_id, events in chargers.items() if is_serial(cp_id)}
    serial_status = {cp_id: events for cp_id, events in status_events.items() if is_serial(cp_id)}
    return serial_chargers, serial_status, control_events, sorted(all_times)

def read_ie_meter_files(ie_files):
    if not ie_files:
        return None
    pts = []
    for fpath in ie_files:
        if not os.path.exists(fpath):
            continue
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    ts = datetime.fromisoformat(obj["timestamp"])
                    pt = float(obj["pt"])
                    pts.append({"timestamp": ts, "pt": pt})
                except Exception:
                    continue
    if not pts:
        return None
    df_ie = pd.DataFrame(pts)
    df_ie['minute'] = df_ie['timestamp'].dt.floor('min')
    df_ie_min = df_ie.groupby('minute').agg({'pt': 'mean'}).reset_index().rename(columns={'minute': 'timestamp', 'pt': 'pt_ie'})
    return df_ie_min

def plot_chargers_and_total_per_day(chargers, status_events, control_events, all_times, df_ie_min, max_total_power=55000):
    # Garante que todos os carregadores detectados (status ou potência) estejam presentes
    cp_ids = sorted(set(list(chargers.keys()) + list(status_events.keys())))
    # Mapeamento dos nomes personalizados
    custom_names = {
        "0000324070000979": "0000324070000979 - 30kW (A)",
        "0000324070001003": "0000324070001003 - 30kW (B)",
        "125020001113": "125020001113 - 7.5kW (A)",
        "125020001122": "125020001122 - 7.5kW (B)",
        "125020001148": "125020001148 - 7.5kW (C)",
        "125020001128": "125020001128 - 7.5kW (D)"
    }
    if not all_times:
        print("Nenhum dado encontrado.")
        return
    days = sorted({t.date() for t in all_times})
    # Tolerância em minutos para considerar que o carregador ainda está carregando após último evento de potência
    TOLERANCIA_MINUTOS = 2
    from datetime import timedelta
    for day in days:
        times_day = [t for t in all_times if t.date() == day]
        last_power = {cp_id: 0 for cp_id in cp_ids}
        last_status = {cp_id: "Available" for cp_id in cp_ids}
        status_idx = {cp_id: 0 for cp_id in cp_ids}
        status_times = {cp_id: status_events.get(cp_id, []) for cp_id in cp_ids}
        power_times = {cp_id: chargers.get(cp_id, []) for cp_id in cp_ids}
        # Para cada carregador, mantém o timestamp do último evento de potência
        last_power_time = {cp_id: None for cp_id in cp_ids}
        data = []
        for t in times_day:
            row = {"timestamp": t}
            total = 0
            for cp_id in cp_ids:
                # Atualiza status
                statuses = status_times.get(cp_id, [])
                while status_idx[cp_id] < len(statuses) and statuses[status_idx[cp_id]]["timestamp"] <= t:
                    last_status[cp_id] = statuses[status_idx[cp_id]]['status']
                    status_idx[cp_id] += 1
                # Atualiza potência se mudou
                pot_event = next((e for e in power_times.get(cp_id, []) if e['timestamp'] == t), None)
                if pot_event is not None:
                    last_power[cp_id] = pot_event['power']
                    last_power_time[cp_id] = t
                # NOVA LÓGICA: Se status não for 'Charging', mas houve evento de potência nos últimos X minutos, considera que está carregando
                carregando = False
                if last_status[cp_id] == 'Charging':
                    carregando = True
                elif last_power_time[cp_id] is not None and (t - last_power_time[cp_id]) <= timedelta(minutes=TOLERANCIA_MINUTOS):
                    carregando = True
                # Só zera se não está carregando
                if not carregando:
                    last_power[cp_id] = 0
                row[cp_id] = last_power[cp_id]
                total += last_power[cp_id]
            row['total_power'] = total
            data.append(row)
        df = pd.DataFrame(data)
        # Agrupa por minuto
        df['minute'] = df['timestamp'].dt.floor('min')
        agg_dict = {cp_id: 'mean' for cp_id in cp_ids}
        agg_dict['total_power'] = 'mean'
        df_min = df.groupby('minute').agg(agg_dict).reset_index().rename(columns={'minute': 'timestamp'})

        # Se não houver dados de potência, adiciona colunas de carregadores com zero
        for cp_id in cp_ids:
            if cp_id not in df_min.columns:
                df_min[cp_id] = 0

        # Gráfico dos carregadores individuais
        fig = go.Figure()
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

        # Adiciona linha do medidor IE se disponível
        # Adiciona linha do medidor IE se disponível (agregado de todos arquivos selecionados)
        if df_ie_min is not None:
            # Filtra apenas dados do dia atual
            df_ie_day = df_ie_min[df_ie_min['timestamp'].dt.date == day]
            if not df_ie_day.empty:
                fig.add_trace(go.Scatter(
                    x=df_ie_day['timestamp'],
                    y=df_ie_day['pt_ie'],
                    mode='lines',
                    name='Potência Ativa Total Medidor IE',
                    line=dict(color='blue', width=2),
                    hovertemplate='Medidor IE<br>Horário: %{x}<br>Potência: %{y} W'
                ))

        # Linha de controle de demanda
        fig.add_shape(
            type='line',
            x0=df_min['timestamp'].min(),
            y0=max_total_power,
            x1=df_min['timestamp'].max(),
            y1=max_total_power,
            line=dict(color='red', width=2, dash='dot'),
        )
        fig.add_trace(go.Scatter(
            x=[df_min['timestamp'].min(), df_min['timestamp'].max()],
            y=[max_total_power, max_total_power],
            mode='lines',
            name='Limite Controle de Demanda',
            line=dict(color='red', width=2, dash='dot'),
            showlegend=True
        ))

        # Marcadores visuais de controle de demanda aplicado
        control_times = [t for t in control_events if t.date() == day]
        control_powers = []
        for t in control_times:
            # Busca potência total mais próxima do timestamp
            match = df_min.iloc[(df_min['timestamp']-t).abs().argsort()[:1]]['total_power']
            if not match.empty:
                control_powers.append(match.values[0])
            else:
                control_powers.append(None)
        fig.add_trace(go.Scatter(
            x=control_times,
            y=control_powers,
            mode='markers',
            name='Controle de Demanda Aplicado',
            marker=dict(color='red', size=16, symbol='star'),
            hovertemplate='Controle de Demanda<br>Horário: %{x}<br>Potência Total: %{y} W'
        ))

        fig.update_layout(
            title=f"Potência Ativa Instantânea dos Carregadores e Total- {day.strftime('%d/%m/%Y')}",
            xaxis_title="Horário",
            yaxis_title="Potência [W]",
            legend_title="Carregadores / Medidor IE / Total",
            hovermode="x unified",
            template="plotly_white"
        )
        fig.show()

if __name__ == "__main__":
    # Inicializa tkinter
    root = tk.Tk()
    root.withdraw()
    # Pergunta via janela se deseja incluir arquivos do medidor IE
    incluir_ie = messagebox.askyesno("Medidor IE", "Deseja incluir arquivos de log do medidor IE na análise?")
    # Seleção do arquivo de log do gateway
    log_file = filedialog.askopenfilename(
        title="Selecione o arquivo de log do gateway",
        filetypes=[("Arquivos de log", "*.log"), ("Todos os arquivos", "*.*")]
    )
    ie_files = []
    if incluir_ie:
        ie_files = filedialog.askopenfilenames(
            title="Selecione um ou mais arquivos de log do medidor IE",
            filetypes=[("Arquivos JSONL", "*.jsonl"), ("Todos os arquivos", "*.*")]
        )
    root.destroy()
    if not log_file:
        print("Nenhum arquivo selecionado.")
    else:
        chargers, status_events, control_events, all_times = parse_log(log_file)
        df_ie_min = read_ie_meter_files(ie_files) if ie_files else None
        # Contar desconexões por dia para todos os carregadores encontrados no log
        disconnect_re = re.compile(r"\[Local Server\] Cliente '([^']+)' desconectado e removido\.")
        disconnects = {}  # {cp_id: {day: count}}
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
        if disconnects:
            print("Resumo de desconexões por carregador:")
            # Remove duplicidades e garante ordenação única
            printed = set()
            for cp_id in sorted(disconnects.keys()):
                if cp_id in printed:
                    continue
                printed.add(cp_id)
                days = disconnects[cp_id]
                print(f"Carregador {cp_id}:")
                for day, count in sorted(days.items()):
                    print(f"  - {day.strftime('%d/%m/%Y')}: {count} vezes")
        else:
            print("Nenhuma desconexão encontrada para os carregadores.")
        plot_chargers_and_total_per_day(chargers, status_events, control_events, all_times, df_ie_min, max_total_power=55000)
