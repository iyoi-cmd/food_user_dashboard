import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="사용자 행동 탐색 대시보드", layout="wide")
st.title("사용자 행동 탐색 대시보드")
st.caption("주별 세션 퍼널 | 플랫폼 · 유입 경로별 | 2025.07 ~ 2026.06")

# ------------------------------------------------------------
# 0. 필요한 데이터만 추출 — 쓸 테이블과 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 처음 한 번만 만듦
# ------------------------------------------------------------
DATA_DIR = "data"                         # 원본 CSV를 넣어 둔 폴더
NEEDED = {                                # 이번 대시보드에 필요한 테이블과 컬럼
    "app_events": ["event_id", "event_datetime", "customer_id", "session_id",
                   "event_name", "platform", "traffic_source"],
}

@st.cache_resource
def get_db():                             # 필요한 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 만듦 (처음 한 번만)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    for name, cols in NEEDED.items():
        pd.read_csv(f"{DATA_DIR}/{name}.csv", usecols=cols).to_sql(name, con, index=False)
    # 5주차 제외 규칙: QA 계정 제외 · 중복 이벤트 제거
    con.execute("""
        CREATE TABLE events AS
        SELECT event_id, event_datetime, customer_id, session_id, event_name, platform, traffic_source
          FROM (SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY session_id, event_datetime, event_name
                                          ORDER BY event_id) AS dup
                  FROM app_events
                 WHERE customer_id NOT LIKE 'QA%')
         WHERE dup = 1
    """)
    # 세션별로 묶는 집계가 빨라지도록 session_id에 인덱스를 붙임 (없으면 첫 실행이 몇 분 걸릴 수 있음)
    con.execute("CREATE INDEX idx_events_session ON events(session_id)")
    return con

# ------------------------------------------------------------
# 1. 데이터 준비 — 정제된 events 테이블을 SQL로 집계
# ------------------------------------------------------------
@st.cache_data
def query(sql):                           # SQL을 실행해 표로 돌려줌
    return pd.read_sql(sql, get_db())

# 주 × 플랫폼 × 유입 경로별 세션 퍼널
SQL_WEEKLY_FUNNEL = """
WITH s AS (                                 -- 세션의 첫 이벤트로 플랫폼 · 유입 경로 · 주 정하기
    SELECT session_id, platform, traffic_source,
           DATE(MIN(event_datetime), 'weekday 0', '-6 days') AS 주시작일
      FROM events
     GROUP BY session_id
)
SELECT s.주시작일,
       s.platform       AS 플랫폼,
       s.traffic_source AS 유입경로,
       COUNT(DISTINCT e.session_id)                                                                 AS 세션수,
       COUNT(DISTINCT CASE WHEN e.event_name = 'view_restaurant' THEN e.session_id END)   AS 조회세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'add_to_cart'     THEN e.session_id END)   AS 장바구니세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'begin_checkout'  THEN e.session_id END)   AS 결제시작세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'order_complete'  THEN e.session_id END)   AS 주문세션,
       SUM(CASE WHEN e.event_name = 'payment_fail' THEN 1 ELSE 0 END)                     AS 결제실패
  FROM events e
  JOIN s ON e.session_id = s.session_id
 WHERE s.주시작일 < '2026-06-29'           -- 이틀치뿐인 마지막 주 제외
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3
"""

@st.cache_data
def load_data():
    data = query(SQL_WEEKLY_FUNNEL)
    data["주시작일"] = pd.to_datetime(data["주시작일"]).dt.date
    return data

df = load_data()                         # 주 × 플랫폼 × 유입 경로별 세션 퍼널

# ------------------------------------------------------------
# 사이드바 필터
# ------------------------------------------------------------
st.sidebar.header("필터")

weeks = sorted(df["주시작일"].unique())
selected_period = st.sidebar.select_slider(
    "기간",
    options=weeks,
    value=(weeks[0], weeks[-1])
)

platforms = sorted(df["플랫폼"].unique())
selected_platforms = st.sidebar.multiselect(
    "플랫폼",
    options=platforms,
    default=platforms
)

traffic_sources = sorted(df["유입경로"].unique())
selected_traffic = st.sidebar.multiselect(
    "유입 경로",
    options=traffic_sources,
    default=traffic_sources
)

# 필터 적용 데이터 f
f = df[
    (df["주시작일"] >= selected_period[0]) &
    (df["주시작일"] <= selected_period[1]) &
    (df["플랫폼"].isin(selected_platforms)) &
    (df["유입경로"].isin(selected_traffic))
]

if f.empty:
    st.warning("선택한 조건에 해당하는 데이터가 없습니다. 필터를 바꿔 주세요.")
    st.stop()

# ------------------------------------------------------------
# 2. KPI 카드
# ------------------------------------------------------------
def rate(a, b):
    return a / b * 100 if b else 0

t = f[["세션수", "결제시작세션", "주문세션"]].sum()
c1, c2, c3 = st.columns(3)
c1.metric("세션 수", f"{t['세션수']:,}개")
c2.metric("주문 전환율 (방문 → 주문)", f"{rate(t['주문세션'], t['세션수']):.1f}%")
c3.metric("결제 전환율 (결제 시작 → 주문)", f"{rate(t['주문세션'], t['결제시작세션']):.1f}%")

st.divider()

# ------------------------------------------------------------
# 3. 차트
# ------------------------------------------------------------
left, right = st.columns(2)

# 3-1. 주별 결제 전환율 (플랫폼별)
wp = f.groupby(["주시작일", "플랫폼"])[["결제시작세션", "주문세션"]].sum().reset_index()
wp["결제전환율"] = (wp["주문세션"] / wp["결제시작세션"] * 100).round(1)
fig1 = px.line(wp, x="주시작일", y="결제전환율", color="플랫폼", markers=True,
               title="주별 결제 전환율 (플랫폼별)")
fig1.update_layout(yaxis_title="결제 전환율 (%)", xaxis_title="")
left.plotly_chart(fig1, width="stretch")

# 3-2. 세션 퍼널
steps = {"1.방문": "세션수", "2.식당조회": "조회세션", "3.장바구니": "장바구니세션",
         "4.결제시작": "결제시작세션", "5.주문완료": "주문세션"}
fun = pd.DataFrame({"단계": list(steps), "세션수": [f[c].sum() for c in steps.values()]})
fig2 = px.funnel(fun, x="세션수", y="단계", title="세션 퍼널")
right.plotly_chart(fig2, width="stretch")

# 3-3. 유입 경로별 주문 전환율
ws = f.groupby("유입경로")[["세션수", "주문세션"]].sum().reset_index()
ws["주문전환율"] = (ws["주문세션"] / ws["세션수"] * 100).round(1)
ws = ws.sort_values("주문전환율")
fig3 = px.bar(ws, x="주문전환율", y="유입경로", orientation="h", text_auto=".1f",
              hover_data=["세션수"], title="유입 경로별 주문 전환율")
fig3.update_xaxes(range=[0, ws["주문전환율"].max() * 1.2 if not ws.empty else 1])
fig3.update_layout(xaxis_title="주문 전환율 (%)", yaxis_title="")
st.plotly_chart(fig3, width="stretch")

st.divider()

# ------------------------------------------------------------
# 4. 집단 비교 영역
# ------------------------------------------------------------
st.subheader("집단 비교")
group_col_map = {"플랫폼": "플랫폼", "유입경로": "유입경로"}
selected_group_label = st.radio("비교 기준", options=list(group_col_map.keys()), horizontal=True)
group_col = group_col_map[selected_group_label]

# 집단별 5단계 퍼널 데이터 집계
group_agg = f.groupby(group_col)[["세션수", "조회세션", "장바구니세션", "결제시작세션", "주문세션"]].sum().reset_index()

# 세션 수가 500개 미만인 집단 확인
low_session_groups = group_agg[group_agg["세션수"] < 500][group_col].tolist()
if low_session_groups:
    st.caption(f"⚠️ 세션 수가 500개 미만인 집단({', '.join(low_session_groups)})이 있어 값이 크게 흔들릴 수 있습니다.")

# 퍼널 단계 매핑
funnel_steps_map = {
    "1.방문": "세션수",
    "2.식당조회": "조회세션",
    "3.장바구니": "장바구니세션",
    "4.결제시작": "결제시작세션",
    "5.주문완료": "주문세션"
}

# 누적 전환율 데이터 프레임 생성 (px.funnel 용)
cum_rows = []
for _, row in group_agg.iterrows():
    g_name = row[group_col]
    base_val = row["세션수"]
    for step_label, col_name in funnel_steps_map.items():
        val = row[col_name]
        pct = (val / base_val * 100) if base_val else 0
        cum_rows.append({
            group_col: g_name,
            "단계": step_label,
            "누적전환율": round(pct, 1),
            "세션수": val
        })
df_cum = pd.DataFrame(cum_rows)

# 단계 전환율 데이터 프레임 생성 (직전 단계 대비 %)
step_rows = []
col_keys = list(funnel_steps_map.values())
step_labels = list(funnel_steps_map.keys())

for _, row in group_agg.iterrows():
    g_name = row[group_col]
    for i in range(1, len(col_keys)):
        prev_col = col_keys[i - 1]
        curr_col = col_keys[i]
        prev_val = row[prev_col]
        curr_val = row[curr_col]
        step_rate = (curr_val / prev_val * 100) if prev_val else 0
        
        step_rows.append({
            group_col: g_name,
            "단계전환": f"{step_labels[i-1]} → {step_labels[i]}",
            "단계전환율": round(step_rate, 1),
            "기준세션수": prev_val
        })
df_step = pd.DataFrame(step_rows)

comp_left, comp_right = st.columns(2)

# 왼쪽: 누적 전환율 퍼널
with comp_left:
    fig_comp_funnel = px.funnel(
        df_cum,
        x="누적전환율",
        y="단계",
        color=group_col,
        title="집단별 누적 전환율 퍼널 (첫 단계 대비 %)",
        hover_data=["세션수"]
    )
    fig_comp_funnel.update_layout(xaxis_title="누적 전환율 (%)", yaxis_title="")
    st.plotly_chart(fig_comp_funnel, width="stretch")

# 오른쪽: 단계별 전환율 막대 (barmode="group")
with comp_right:
    fig_comp_bar = px.bar(
        df_step,
        x="단계전환",
        y="단계전환율",
        color=group_col,
        barmode="group",
        text_auto=".1f",
        hover_data=["기준세션수"],
        title="집단별 단계 전환율 (직전 단계 대비 %)"
    )
    fig_comp_bar.update_layout(yaxis_title="단계 전환율 (%)", xaxis_title="")
    st.plotly_chart(fig_comp_bar, width="stretch")

st.divider()

# ------------------------------------------------------------
# 5. 드릴다운: 언제부터, 무엇 때문일까 영역
# ------------------------------------------------------------
st.subheader("드릴다운: 언제부터, 무엇 때문일까")

dd_col1, dd_col2 = st.columns(2)

available_groups = sorted(f[group_col].unique())
with dd_col1:
    selected_sub_group = st.selectbox("자세히 볼 집단", options=available_groups)

step_mapping_dd = {
    "방문 → 식당조회": ("세션수", "조회세션"),
    "식당조회 → 장바구니": ("조회세션", "장바구니세션"),
    "장바구니 → 결제시작": ("장바구니세션", "결제시작세션"),
    "결제시작 → 주문": ("결제시작세션", "주문세션")
}
with dd_col2:
    selected_step_label = st.selectbox("단계", options=list(step_mapping_dd.keys()))

prev_col_name, curr_col_name = step_mapping_dd[selected_step_label]
other_col = "유입경로" if group_col == "플랫폼" else "플랫폼"

# 차트 ① 데이터 준비
df_sub = f[f[group_col] == selected_sub_group]
df_weekly = df_sub.groupby(["주시작일", other_col])[[prev_col_name, curr_col_name]].sum().reset_index()

# 직전 단계 세션이 0인 주는 빈 값으로 처리하기 위해 결측 처리 적용
df_weekly["전환율"] = df_weekly.apply(
    lambda row: round(row[curr_col_name] / row[prev_col_name] * 100, 1) if row[prev_col_name] > 0 else None,
    axis=1
)

fig_dd1 = px.line(
    df_weekly,
    x="주시작일",
    y="전환율",
    color=other_col,
    markers=True,
    title=f"① {selected_sub_group}의 주별 {selected_step_label} 전환율 — {other_col}별로 쪼개 보기"
)
fig_dd1.update_layout(yaxis_title="전환율 (%)", xaxis_title="")
st.plotly_chart(fig_dd1, width="stretch")
st.caption("모든 갈래가 같은 시점에 함께 움직였다면, 그 축은 원인이 아닙니다.")

# 차트 ② 데이터 준비
f_copy = f.copy()
f_copy["구분"] = f_copy[group_col].apply(lambda x: selected_sub_group if x == selected_sub_group else "그 외")

df_fail = f_copy.groupby(["주시작일", "구분"])["결제실패"].sum().reset_index()

# 색상 지정: 고른 집단은 진한 빨간색, 그 외는 회색
color_map = {selected_sub_group: "#d62728", "그 외": "#7f7f7f"}

fig_dd2 = px.bar(
    df_fail,
    x="주시작일",
    y="결제실패",
    color="구분",
    barmode="group",
    color_discrete_map=color_map,
    title=f"주별 결제 실패 건수 ({selected_sub_group} vs 그 외)"
)
fig_dd2.update_layout(yaxis_title="결제 실패 건수", xaxis_title="")
st.plotly_chart(fig_dd2, width="stretch")
st.caption("문제가 생긴 시점과 함께 움직였다면 원인 후보입니다. 함께 움직였다고 원인이 확정되지는 않습니다.")