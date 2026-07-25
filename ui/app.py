import streamlit as st
import pandas as pd
import json
import plotly.graph_objects as go
import time
import os

st.set_page_config(layout="wide", page_title="Envelope BMS Dashboard")
st.title("Project Envelope: AI-Gated BMS")

AI_LOG = "logs/ai/control_log.jsonl"
BASELINE_LOG = "logs/baseline/control_log.jsonl"

metric_placeholder = st.sidebar.empty()
chart_placeholder = st.empty()
energy_placeholder = st.empty()
log_placeholder = st.empty()


def load_log(path):
    data = []
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if str(row.get('reason')) == '0' or not row.get('reason'):
                        row['reason'] = "Waiting..."
                    data.append(row)
                except Exception:
                    continue
    df = pd.DataFrame(data)
    for col in ['step', 'source', 'setpoint', 'reason', 't_in', 't_out', 'cumulative_kwh']:
        if col not in df.columns:
            df[col] = None

    # FIX: with the energyplus_bridge.py dedupe fix, 'step' should already
    # be a clean 1-per-simulated-instant counter. Keep this as a defensive
    # backstop for logs recorded before that fix (or from any other source
    # of duplicate rows) — collapse to one row per step, keeping the last.
    if not df.empty and df['step'].notna().any():
        df['step'] = pd.to_numeric(df['step'], errors='coerce')
        df = df.dropna(subset=['step']).sort_values('step')
        df = df.drop_duplicates(subset='step', keep='last').reset_index(drop=True)
    return df


def align_on_shared_steps(ai_df, baseline_df):
    """
    FIX: previously the AI and baseline logs were compared by DataFrame
    row index, which silently assumes both instances have advanced the
    same amount of simulated time by row N — they don't, since each
    process free-runs independently. This trims both to the steps they
    actually have in common, so 'Live savings vs baseline' reflects the
    same simulated window on both sides instead of comparing two
    different points in the weather file.
    """
    if ai_df.empty or baseline_df.empty:
        return ai_df, baseline_df
    max_shared_step = min(ai_df['step'].max(), baseline_df['step'].max())
    ai_aligned = ai_df[ai_df['step'] <= max_shared_step]
    baseline_aligned = baseline_df[baseline_df['step'] <= max_shared_step]
    return ai_aligned, baseline_aligned


while True:
    ai_df = load_log(AI_LOG)
    baseline_df = load_log(BASELINE_LOG)
    ai_aligned, baseline_aligned = align_on_shared_steps(ai_df, baseline_df)

    if not ai_df.empty and len(ai_df) > 2:
        # 1. Sidebar metrics (AI instance)
        with metric_placeholder.container():
            st.metric("Indoor Temp (AI)", f"{ai_df['t_in'].iloc[-1]:.2f} °C")
            st.metric("Outdoor Temp", f"{ai_df['t_out'].iloc[-1]:.2f} °C")
            st.write(f"AI Control Step: {ai_df['step'].iloc[-1]}")
            if not baseline_df.empty:
                st.write(f"Baseline Control Step: {baseline_df['step'].iloc[-1]}")
                st.caption(
                    f"Compared range: step 0 – "
                    f"{int(ai_aligned['step'].max()) if not ai_aligned.empty else 0} "
                    f"(overlap of both runs)"
                )
            else:
                st.write("Baseline: not running — start it with `python main.py --baseline`")

        # 2. Overlay chart — Feature 2: baseline vs AI on the same chart.
        # FIX: explicit x=...['step'] instead of relying on row index, so
        # the axis reflects real simulated step even if either log has
        # gaps (e.g. from the dedupe above).
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ai_df['step'], y=ai_df['t_in'], name="AI Indoor",
                                  line=dict(color='orange')))
        fig.add_trace(go.Scatter(x=ai_df['step'], y=ai_df['t_out'], name="Outdoor",
                                  line=dict(color='deepskyblue', dash='dot')))
        fig.add_trace(go.Scatter(x=ai_df['step'], y=ai_df['setpoint'], name="AI Setpoint",
                                  line=dict(color='lime', shape='hv')))
        if not baseline_df.empty:
            fig.add_trace(go.Scatter(x=baseline_df['step'], y=baseline_df['t_in'], name="Baseline Indoor",
                                      line=dict(color='hotpink', dash='dash')))
            fig.add_trace(go.Scatter(x=baseline_df['step'], y=baseline_df['setpoint'], name="Baseline Setpoint",
                                      line=dict(color='violet', dash='dot', shape='hv')))

        fig.update_layout(
            title="Live Building Physics: AI vs Baseline",
            height=450,
            template="plotly_dark",
            xaxis_title="Simulation Step",
        )
        chart_placeholder.plotly_chart(fig, width='stretch', key=f"chart_{time.time_ns()}")

        # 3. Live energy overlay + savings number (Feature 2's headline metric)
        # FIX: uses ai_aligned / baseline_aligned (trimmed to the shared
        # step range) instead of the raw full-length logs, so the savings
        # number compares the same simulated window on both sides.
        with energy_placeholder.container():
            st.write("### Energy: AI vs Baseline")
            ai_kwh_series = pd.to_numeric(ai_aligned['cumulative_kwh'], errors='coerce').dropna() \
                if not ai_aligned.empty else pd.Series(dtype=float)
            if not ai_kwh_series.empty and not baseline_aligned.empty:
                base_kwh_series = pd.to_numeric(baseline_aligned['cumulative_kwh'], errors='coerce').dropna()
                if not base_kwh_series.empty:
                    ai_kwh = ai_kwh_series.iloc[-1]
                    base_kwh = base_kwh_series.iloc[-1]
                    col1, col2, col3 = st.columns(3)
                    col1.metric("AI cumulative energy (aligned)", f"{ai_kwh:.2f} kWh")
                    col2.metric("Baseline cumulative energy (aligned)", f"{base_kwh:.2f} kWh")
                    if base_kwh > 0:
                        savings_pct = (base_kwh - ai_kwh) / base_kwh * 100
                        col3.metric("Live savings vs baseline", f"{savings_pct:.1f}%")
                    else:
                        col3.metric("Live savings vs baseline", "N/A")

                    e_fig = go.Figure()
                    e_fig.add_trace(go.Scatter(x=ai_aligned['step'], y=ai_kwh_series,
                                                name="AI kWh", line=dict(color='orange')))
                    e_fig.add_trace(go.Scatter(x=baseline_aligned['step'], y=base_kwh_series,
                                                name="Baseline kWh", line=dict(color='hotpink')))
                    e_fig.update_layout(height=300, template="plotly_dark",
                                         xaxis_title="Simulation Step", yaxis_title="Cumulative kWh")
                    st.plotly_chart(e_fig, width='stretch', key=f"energy_{time.time_ns()}")
            else:
                st.info("Energy tracking data not yet available, or the AI and "
                        "baseline runs don't have any overlapping simulated-step "
                        "range yet. If this stays empty once both instances are "
                        "running, check the console output for a facility-meter "
                        "warning from core/energyplus_bridge.py.")

        # 4. Decision log
        with log_placeholder.container():
            st.write("### Explainable AI Decisions")
            display_df = ai_df[ai_df['source'].isin(['AI', 'AI (Corrected)', 'FAILSAFE', 'FAILSAFE (gate override)'])].tail(5)
            if not display_df.empty:
                st.table(display_df[['step', 'source', 'setpoint', 'reason']])

    time.sleep(2)