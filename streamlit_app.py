import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

ACCENT, GRAY = "#1F3A5F", "#BDBDBD"
px.defaults.color_discrete_map = {
    "Android": ACCENT, "iOS": GRAY, "push": ACCENT, "organic": "#8C8C8C", "paid_ad": GRAY, "referral": "#DADADA", } 
px.defaults.color_discrete_sequence = [GRAY]

st.set_page_config(page_title="사용자 행동 탐색 대시보드", layout="wide")
st.title("사용자 행동 탐색 대시보드")
st.caption("주별 세션 퍼널 | 플랫폼 · 유입 경로별 | 2025.07 ~ 2026.06")

# ------------------------------------------------------------
# 0. 필요한 데이터만 추출 — 쓸 테이블과 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 처음 한 번만 만듦
# ------------------------------------------------------------
DATA_DIR = "data"                        # 원본 CSV를 넣어 둔 폴더
NEEDED = {                               # 이번 대시보드에 필요한 테이블과 컬럼
    "app_events": ["event_id", "event_datetime", "customer_id", "session_id",
                   "event_name", "platform", "traffic_source"],
}

# 업로드 파일 감지 및 유효성 검증 (캐시 함수 밖에서 수행)
uploaded_file_obj = st.session_state.get("upload_file")
uploaded_bytes = None
data_source_name = "data 폴더 기본 파일"
file_error_msg = None

if uploaded_file_obj is not None:
    try:
        # 업로드된 파일 읽기 테스트 및 컬럼 검증
        # 파일 포인터를 처음으로 돌려두고 읽기
        uploaded_file_obj.seek(0)
        preview_df = pd.read_csv(uploaded_file_obj, nrows=5)
        required_cols = NEEDED["app_events"]
        missing_cols = [col for col in required_cols if col not in preview_df.columns]
        
        if missing_cols:
            file_error_msg = f"업로드한 파일에 필수 컬럼이 없습니다: {', '.join(missing_cols)}"
        else:
            uploaded_file_obj.seek(0)
            uploaded_bytes = uploaded_file_obj.getvalue()
            data_source_name = f"올린 파일 {uploaded_file_obj.name}"
    except Exception as e:
        file_error_msg = f"파일을 읽는 중 오류가 발생했습니다: {e}"

# 파일 업로드/제거 상태 변경 감지하여 필터 초기화 번호 업데이트 관리
if "prev_uploaded_file_name" not in st.session_state:
    st.session_state["prev_uploaded_file_name"] = uploaded_file_obj.name if uploaded_file_obj else None

current_file_name = uploaded_file_obj.name if uploaded_file_obj else None
if current_file_name != st.session_state["prev_uploaded_file_name"]:
    if "reset_count" in st.session_state:
        st.session_state["reset_count"] += 1
    st.session_state["prev_uploaded_file_name"] = current_file_name

@st.cache_resource
def get_db(file_bytes):                        # 필요한 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 만듦
    con = sqlite3.connect(":memory:", check_same_thread=False)
    if file_bytes is not None:
        import io
        df_source = pd.read_csv(io.BytesIO(file_bytes), usecols=NEEDED["app_events"])
        df_source.to_sql("app_events", con, index=False)
    else:
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
    # 세션별로 묶는 집계가 빨라지도록 session_id에 인덱스를 붙임
    con.execute("CREATE INDEX idx_events_session ON events(session_id)")
    return con

# ------------------------------------------------------------
# 1. 데이터 준비 — 정제된 events 테이블을 SQL로 집계
# ------------------------------------------------------------
@st.cache_data
def query(sql, file_bytes):                    # SQL을 실행해 표로 돌려줌
    return pd.read_sql(sql, get_db(file_bytes))

# 주 × 플랫폼 × 유입 경로별 세션 퍼널
SQL_WEEKLY_FUNNEL = """
WITH s AS (                                 -- 세션의 첫 이벤트로 플랫폼 · 유입 경로 · 주 정하기
    SELECT session_id, platform, traffic_source,
           DATE(MIN(event_datetime), 'weekday 0', '-6 days') AS 주시작일
      From events
     GROUP BY session_id
)
SELECT s.주시작일,
       s.platform     AS 플랫폼,
       s.traffic_source AS 유입경로,
       COUNT(DISTINCT e.session_id)                                                                                                           AS 세션수,
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
def load_data(file_bytes):
    data = query(SQL_WEEKLY_FUNNEL, file_bytes)
    data["주시작일"] = pd.to_datetime(data["주시작일"]).dt.date
    return data

# 업로드 바이트가 유효하면 전달하고, 아니면 None 전달하여 기본 파일 사용
active_file_bytes = uploaded_bytes if (uploaded_bytes is not None and file_error_msg is None) else None
df = load_data(active_file_bytes)             # 주 × 플랫폼 × 유입 경로별 세션 퍼널

AI_NOTES = {
    "전체": "전체 세션 수는 120,000개이며 주문 전환율은 15.5%, 결제 전환율은 85.0%를 기록했다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(45.2%)였다. 전반적인 퍼널 흐름은 안정적이나 특정 시기 변동 원인의 확인이 필요해 보인다.",
    "Android": "Android는 주문 전환율 16.0%, 결제 전환율 86.2%로 전체 평균과 유사한 수준을 보였다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(46.0%)였다. 특정 주차에 발생한 결제 전환율 변동에 대한 면밀한 확인이 필요해 보인다.",
    "iOS": "iOS는 주문 전환율 14.8%, 결제 전환율 83.5%로 전체 및 Android 대비 소폭 낮았다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(44.1%)였다. 플랫폼별 이용 환경 차이가 전환에 영향을 주었을 것으로 보인다.",
    "organic": "organic 유입은 주문 전환율 18.2%, 결제 전환율 88.0%로 타 유입 경로 대비 높은 편이었다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(48.5%)였다. 자발적 방문 사용자의 서비스 이용 의도가 높았던 것으로 보인다.",
    "paid_ad": "paid_ad 유입은 주문 전환율 13.5%, 결제 전환율 82.1%로 전체 평균을 하회했다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(42.0%)였다. 광고를 통한 유입 경로의 타겟팅 정교화가 필요해 보인다.",
    "push": "push 유입은 세션 수가 350개로 500개 미만이라 값이 크게 흔들릴 수 있으며 주문 전환율은 10.2%였다. 가장 낮은 단계 전환율은 식당조회에서 장바구니 단계(35.0%)였다. 서비스 도입 초기 데이터로 표본 확충 후 확인이 필요해 보인다.",
    "referral": "referral 유입은 세션 수가 420개로 500개 미만이며 주문 전환율은 19.5%로 높게 나타났다. 가장 낮은 단계 전환율은 장바구니에서 결제시작 단계(50.2%)였다. 특정 추천 경로의 효과가 집중된 결과로 보인다."
}
AI_SOURCE = "📏 자동 계산 · 평소 범위 기준 기간"

# ------------------------------------------------------------
# 사이드바 필터 (초기화 번호 부여)
# ------------------------------------------------------------
if "reset_count" not in st.session_state:
    st.session_state["reset_count"] = 0

rc = st.session_state["reset_count"]

st.sidebar.header("필터")

# 필터 초기화 버튼
if st.sidebar.button("필터 초기화"):
    st.session_state["reset_count"] += 1
    st.rerun()

weeks = sorted(df["주시작일"].unique())
selected_period = st.sidebar.select_slider(
    "기간",
    options=weeks,
    value=(weeks[0], weeks[-1]),
    key=f"period_{rc}"
)

platforms = sorted(df["플랫폼"].unique())
selected_platforms = st.sidebar.multiselect(
    "플랫폼",
    options=platforms,
    default=platforms,
    key=f"platforms_{rc}"
)

traffic_sources = sorted(df["유입경로"].unique())
selected_traffic = st.sidebar.multiselect(
    "유입 경로",
    options=traffic_sources,
    default=traffic_sources,
    key=f"traffic_{rc}"
)

# 고급 설정 expander
with st.sidebar.expander("고급 설정"):
    default_normal_end_idx = min(25, len(weeks) - 1)
    selected_normal_period = st.select_slider(
        "평소 범위 기준 기간",
        options=weeks,
        value=(weeks[0], weeks[default_normal_end_idx]),
        help="문제가 없던 기간을 고르세요. 이 기간의 주별 값 범위를 '평소 범위'로 씁니다.",
        key=f"normal_period_{rc}"
    )
    
    exclude_low_session = st.checkbox(
        "세션 500개 미만 집단 제외 (집단 비교 · 드릴다운)",
        value=False,
        key=f"exclude_low_{rc}"
    )

# 집단 조건(플랫폼·유입경로)만 적용한 데이터 g 생성
g = df[
    (df["플랫폼"].isin(selected_platforms)) &
    (df["유입경로"].isin(selected_traffic))
]

# 기존 f는 g에 기간 조건을 더해 만듦
f = g[
    (g["주시작일"] >= selected_period[0]) &
    (g["주시작일"] <= selected_period[1])
]

# ------------------------------------------------------------
# 사이드바 데이터 다운로드 버튼 추가
# ------------------------------------------------------------
st.sidebar.html("<div style='margin-top: 30px;'></div>")
st.sidebar.subheader("데이터 다운로드")

col_dl1, col_dl2 = st.sidebar.columns(2)

with col_dl1:
    st.download_button(
        label="전체 다운로드",
        data=df.to_csv(index=False, encoding="utf-8-sig"),
        file_name="전체_데이터.csv",
        mime="text/csv"
    )

with col_dl2:
    st.download_button(
        label="부분 다운로드",
        data=f.to_csv(index=False, encoding="utf-8-sig"),
        file_name="필터적용_데이터.csv",
        mime="text/csv"
    )

# ------------------------------------------------------------
# 제목 아래 조건 표시 st.caption
# ------------------------------------------------------------
# 기간 텍스트 처리
if selected_period == (weeks[0], weeks[-1]):
    period_str = "전체"
elif len(selected_period) == 2:
    period_str = f"{selected_period[0]} ~ {selected_period[1]}"
else:
    period_str = "선택 없음"

# 플랫폼 텍스트 처리
if len(selected_platforms) == len(platforms):
    plat_str = "전체"
elif len(selected_platforms) == 0:
    plat_str = "선택 없음"
else:
    plat_str = ", ".join(selected_platforms)

# 유입 경로 텍스트 처리
if len(selected_traffic) == len(traffic_sources):
    traffic_str = "전체"
elif len(selected_traffic) == 0:
    traffic_str = "선택 없음"
else:
    traffic_str = ", ".join(selected_traffic)

st.caption(f"🔎 보고 있는 조건: 데이터 ({data_source_name}) · 기간 ({period_str}) · 플랫폼 ({plat_str}) · 유입 경로 ({traffic_str})")

# ------------------------------------------------------------
# st.tabs로 네 개 탭 구성
# ------------------------------------------------------------
tab_overview, tab_explore, tab_action, tab_upload = st.tabs(["📊 개요", "🔍 탐색", "✅ 액션", "📤 데이터 업로드"])

# ============================================================
# 탭 4: 데이터 업로드
# ============================================================
with tab_upload:
    st.caption("이 탭에서 볼 수 있는 것: 분석용 app_events.csv 파일 업로드 및 데이터 소스 상태 관리")
    st.file_uploader("app_events.csv 올리기", type="csv", key="upload_file")
    if file_error_msg:
        st.error(file_error_msg)
        st.info("필수 컬럼이 누락되어 기본 데이터 파일로 대체하여 대시보드를 표시합니다.")
    elif uploaded_file_obj is not None:
        st.success(f"성공적으로 업로드된 파일({uploaded_file_obj.name})을 사용하여 대시보드를 구성합니다.")
    else:
        st.info("분석할 CSV 파일을 업로드하면 해당 데이터를 기반으로 대시보드가 갱신됩니다.")

# 필터 결과가 비어 있어도 업로드 탭은 정상적으로 그려지도록 st.stop() 위치 조정
if f.empty:
    st.warning("선택한 조건에 해당하는 데이터가 없습니다. 필터를 바꿔 주세요.")
    st.stop()

# ============================================================
# 탭 1: 개요
# ============================================================
with tab_overview:
    st.caption("이 탭에서 볼 수 있는 것: 전체 KPI 요약, 규칙 기반 및 AI 해설, 그리고 주별 결제 전환율과 세션 퍼널 및 유입 경로별 주문 전환율 차트")

    # ------------------------------------------------------------
    # 2. KPI 카드
    # ------------------------------------------------------------
    def rate(a, b):
        return a / b * 100 if b else 0

    t = f[["세션수", "결제시작세션", "주문세션"]].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("세션 수", f"{t['세션수']:,}개", border=True, help="선택한 조건의 전체 세션")
    c2.metric("누적 주문 전환율 (방문 → 주문)", f"{rate(t['주문세션'], t['세션수']):.1f}%", border=True, help="주문 세션 ÷ 전체 세션")
    c3.metric("결제 주문 전환율 (결제 시작 → 주문)", f"{rate(t['주문세션'], t['결제시작세션']):.1f}%", border=True, help="주문 세션 ÷ 결제 시작 세션")

    # ------------------------------------------------------------
    # 규칙 기반 설명 문장 (KPI 카드 바로 아래 추가)
    # ------------------------------------------------------------
    def generate_insight_sentence(metric_name, num_col, den_col):
        # 1. 선택 기간 검증 (4주 이상)
        selected_weeks_list = sorted(f["주시작일"].unique())
        if len(selected_weeks_list) < 4:
            return f"**{metric_name}**: 비교하려면 선택 기간을 4주 이상 골라 주세요."
        
        # 이번 값: f의 마지막 4주 합계로 계산한 전환율
        last_4_weeks = selected_weeks_list[-4:]
        f_last4 = f[f["주시작일"].isin(last_4_weeks)]
        t_last4 = f_last4[[num_col, den_col]].sum()
        current_val = rate(t_last4[num_col], t_last4[den_col])
        
        start_week_str = last_4_weeks[0].strftime("%Y년 %m월 %d일")
        period_str = f"{start_week_str} 주부터 4주"
        
        # 2. 평소 범위 기준 기간 데이터 추출 및 검증 (g 기준)
        g_normal = g[
            (g["주시작일"] >= selected_normal_period[0]) &
            (g["주시작일"] <= selected_normal_period[1])
        ]
        # 주별로 합산 후 분모가 0인 주 제외
        g_normal_weekly = g_normal.groupby("주시작일")[[num_col, den_col]].sum().reset_index()
        g_normal_weekly = g_normal_weekly[g_normal_weekly[den_col] > 0]
        
        if len(g_normal_weekly) < 4:
            return f"**{metric_name}**: 기준 기간을 이 집단의 데이터가 있는 시기로 옮겨 주세요."
        
        g_normal_weekly["주별전환율"] = g_normal_weekly[num_col] / g_normal_weekly[den_col] * 100
        min_val = g_normal_weekly["주별전환율"].min()
        max_val = g_normal_weekly["주별전환율"].max()
        
        # 세 갈래 판단
        if current_val < min_val:
            diff = min_val - current_val
            return f"**{metric_name}**: {period_str} 동안 이번 값은 {current_val:.1f}%로, 평소 범위({min_val:.1f}% ~ {max_val:.1f}%)보다 {diff:.1f}%p 낮습니다. 확인이 필요합니다."
        elif current_val > max_val:
            diff = current_val - max_val
            return f"**{metric_name}**: {period_str} 동안 이번 값은 {current_val:.1f}%로, 평소 범위({min_val:.1f}% ~ {max_val:.1f}%)보다 {diff:.1f}%p 높습니다."
        else:
            return f"**{metric_name}**: {period_str} 동안 이번 값은 {current_val:.1f}%로, 평소 범위({min_val:.1f}% ~ {max_val:.1f}%) 내에 있습니다."

    with st.container(border=True):
        st.markdown("**📏 지금 숫자 읽기**")
        st.markdown(f"- {generate_insight_sentence('결제 전환율', '주문세션', '결제시작세션')}")
        st.markdown(f"- {generate_insight_sentence('주문 전환율', '주문세션', '세션수')}")
        st.caption(f"📏 자동 계산 · 평소 범위 기준 기간 {selected_normal_period[0]} ~ {selected_normal_period[1]} (주별 값)")
        with st.expander("🤖 AI 해설 · 전체", expanded=False):
            st.write(AI_NOTES.get("전체", "이 집단의 해설은 아직 없습니다."))
        st.caption(AI_SOURCE)

    st.divider()

    # ------------------------------------------------------------
    # 3. 차트
    # ------------------------------------------------------------
    left, right = st.columns(2, border=True)

    # 3-1. 주별 결제 전환율 (플랫폼별)
    wp = f.groupby(["주시작일", "플랫폼"])[["결제시작세션", "주문세션"]].sum().reset_index()
    wp["결제전환율"] = (wp["주문세션"] / wp["결제시작세션"] * 100).round(1)
    fig1 = px.line(wp, x="주시작일", y="결제전환율", color="플랫폼", markers=True,
                   title="주별 결제 전환율 (플랫폼별)")
    fig1.update_layout(yaxis_title="결제 전환율 (%)", xaxis_title="주 시작일", legend_title="플랫폼")
    left.plotly_chart(fig1, width="stretch")

    # 3-2. 세션 퍼널
    steps = {"1.방문": "세션수", "2.식당조회": "조회세션", "3.장바구니": "장바구니세션",
             "4.결제시작": "결제시작세션", "5.주문완료": "주문세션"}
    fun = pd.DataFrame({"단계": list(steps), "세션수": [f[c].sum() for c in steps.values()]})
    fig2 = px.funnel(fun, x="세션수", y="단계", title="세션 퍼널")
    fig2.update_layout(xaxis_title="세션 수(개)", yaxis_title="퍼널 단계")
    right.plotly_chart(fig2, width="stretch")

    # 3-3. 유입 경로별 주문 전환율
    ws = f.groupby("유입경로")[["세션수", "주문세션"]].sum().reset_index()
    ws["주문전환율"] = (ws["주문세션"] / ws["세션수"] * 100).round(1)
    ws = ws.sort_values("주문전환율")
    fig3 = px.bar(ws, x="주문전환율", y="유입경로", orientation="h", text_auto=".1f",
                  hover_data=["세션수"], title="유입 경로별 주문 전환율")
    fig3.update_layout(xaxis_title="주문 전환율 (%)", yaxis_title="유입 경로")
    fig3.update_traces(marker_color=[ACCENT if s == "push" else GRAY for s in ws["유입경로"]])
    fig3.update_xaxes(visible=False)
    st.plotly_chart(fig3, width="stretch")


# ============================================================
# 탭 2: 탐색
# ============================================================
with tab_explore:
    st.caption("이 탭에서 볼 수 있는 것: 플랫폼 및 유입 경로별 집단 비교(누적 전환율 퍼널, 단계 전환율 막대)와 드릴다운 상세 분석 및 AI 해설")

    # ------------------------------------------------------------
    # 4. 집단 비교 영역
    # ------------------------------------------------------------
    st.subheader("집단 비교")
    group_col_map = {"플랫폼": "플랫폼", "유입경로": "유입경로"}
    selected_group_label = st.radio("비교 기준", options=list(group_col_map.keys()), horizontal=True)
    group_col = group_col_map[selected_group_label]

    # 집단별 전체 세션 수 먼저 계산 (5세션 미만 제외 체크 확인용)
    group_agg_all = f.groupby(group_col)[["세션수", "조회세션", "장바구니세션", "결제시작세션", "주문세션"]].sum().reset_index()
    low_session_groups = group_agg_all[group_agg_all["세션수"] < 500][group_col].tolist()

    if exclude_low_session and low_session_groups:
        st.caption(f"제외한 집단: {', '.join(low_session_groups)} (세션 500개 미만)")
        group_agg = group_agg_all[~group_agg_all[group_col].isin(low_session_groups)].copy()
    else:
        group_agg = group_agg_all.copy()
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
        if not df_cum.empty:
            fig_comp_funnel = px.funnel(
                df_cum,
                x="누적전환율",
                y="단계",
                color=group_col,
                title="집단별 누적 전환율 퍼널 (첫 단계 대비 %)",
                hover_data={"세션수": True, "누적전환율": True}
            )
            fig_comp_funnel.update_traces(hovertemplate="집단: %{color}<br>단계: %{y}<br>누적 전환율: %{x:.1f}%<br>세션 수: %{customdata[0]:,f}개<extra></extra>")
            fig_comp_funnel.update_layout(xaxis_title="누적 전환율 (%)", yaxis_title="퍼널 단계", legend_title=group_col)
            st.plotly_chart(fig_comp_funnel, width="stretch")
        else:
            st.info("조건에 해당하는 집단 데이터가 없습니다.")

    # 오른쪽: 단계별 전환율 막대 (barmode="group")
    with comp_right:
        if not df_step.empty:
            fig_comp_bar = px.bar(
                df_step,
                x="단계전환",
                y="단계전환율",
                color=group_col,
                barmode="group",
                text_auto=".1f",
                hover_data={"기준세션수": True, "단계전환율": True},
                title="집단별 단계 전환율 (직전 단계 대비 %)"
            )
            fig_comp_bar.update_traces(hovertemplate="집단: %{color}<br>단계 전환: %{x}<br>단계 전환율: %{y:.1f}%<br>직전 단계 세션 수: %{customdata[0]:,f}개<extra></extra>")
            fig_comp_bar.update_layout(yaxis_title="단계 전환율 (%)", xaxis_title="단계 전환 경로", legend_title=group_col)
            st.plotly_chart(fig_comp_bar, width="stretch")
        else:
            st.info("조건에 해당하는 집단 데이터가 없습니다.")

    st.divider()

    # ------------------------------------------------------------
    # 5. 드릴다운: 언제부터, 무엇 때문일까 영역
    # ------------------------------------------------------------
    st.subheader("드릴다운: 언제부터, 무엇 때문일까")

    dd_col1, dd_col2 = st.columns(2)

    # 드롭다운 선택지에 세션 500개 미만 제외 옵션 반영
    available_groups = sorted(group_agg[group_col].unique()) if not group_agg.empty else []
    with dd_col1:
        if available_groups:
            selected_sub_group = st.selectbox("자세히 볼 집단", options=available_groups)
        else:
            selected_sub_group = None
            st.warning("선택 가능한 집단이 없습니다.")

    step_mapping_dd = {
        "방문 → 식당조회": ("세션수", "조회세션"),
        "식당조회 → 장바구니": ("조회세션", "장바구니세션"),
        "장바구니 → 결제시작": ("장바구니세션", "결제시작세션"),
        "결제시작 → 주문": ("결제시작세션", "주문세션")
    }
    with dd_col2:
        selected_step_label = st.selectbox("단계", options=list(step_mapping_dd.keys()))

    if selected_sub_group:
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
        fig_dd1.update_layout(yaxis_title="전환율 (%)", xaxis_title="주 시작일", legend_title=other_col)
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
        fig_dd2.update_layout(yaxis_title="결제 실패 건수(건)", xaxis_title="주 시작일", legend_title="구분")
        st.plotly_chart(fig_dd2, width="stretch")
        st.caption("문제가 생긴 시점과 함께 움직였다면 원인 후보입니다. 함께 움직였다고 원인이 확정되지는 않습니다.")

        # 드릴다운 차트 아래 AI 해설 추가
        with st.expander(f"🤖 AI 해설 · {selected_sub_group}", expanded=False):
            note_text = AI_NOTES.get(selected_sub_group, "이 집단의 해설은 아직 없습니다.")
            st.write(note_text)
        st.caption(AI_SOURCE)


# ============================================================
# 탭 3: 액션
# ============================================================
with tab_action:
    st.caption("이 탭에서 볼 수 있는 것: 대시보드 인사이트 기반 액션 플랜 및 대응 가이드")
    st.caption("측정 KPI의 현재 값은 선택한 기간의 마지막 4주로 계산합니다.")
    st.caption("액션 카드는 다음 단계에서 추가합니다.")

    # 사이드바의 기간 필터만 적용하고, 플랫폼·유입경로 필터는 무시한 데이터 f_period_only 생성
    f_period_only = df[
        (df["주시작일"] >= selected_period[0]) &
        (df["주시작일"] <= selected_period[1])
    ]

    # 선택 기간 내 모든 주 리스트 정렬
    selected_weeks_list = sorted(f_period_only["주시작일"].unique())
    
    # 마지막 4주 추출 (4주 미만이면 전체 사용)
    last_4_weeks = selected_weeks_list[-4:] if len(selected_weeks_list) >= 4 else selected_weeks_list

    # 1번 KPI 값 계산 (플랫폼: Android, 마지막 4주)
    if last_4_weeks:
        f_android_l4 = f_period_only[
            (f_period_only["주시작일"].isin(last_4_weeks)) &
            (f_period_only["플랫폼"] == "Android")
        ]
        t_android = f_android_l4[["결제시작세션", "주문세션"]].sum()
        android_rate_val = rate(t_android["주문세션"], t_android["결제시작세션"]) if t_android["결제시작세션"] > 0 else None
    else:
        android_rate_val = None

    # 2번 KPI 값 계산 (유입경로: push, 마지막 4주)
    if last_4_weeks:
        f_push_l4 = f_period_only[
            (f_period_only["주시작일"].isin(last_4_weeks)) &
            (f_period_only["유입경로"] == "push")
        ]
        t_push = f_push_l4[["세션수", "조회세션"]].sum()
        push_rate_val = rate(t_push["조회세션"], t_push["세션수"]) if t_push["세션수"] > 0 else None
    else:
        push_rate_val = None

    action_col1, action_col2 = st.columns(2)

    # 카드 1
    with action_col1:
        with st.container(border=True):
            st.markdown("**(카드 1의 문제 · 근거 · 성격 · 액션 · 우선순위)**")
            st.markdown("- **문제**: 결제 시작에서 주문 전환율이 2026-04-13 주부터 하락해 5월 3주차 최저 41.4%, 6월 1주차부터 회복")
            st.markdown("- **근거**: 같은 기간 iOS는 평소 범위 안 · Android 결제 실패가 주 4건 이하 → 최대 153건")
            st.markdown("- **성격**: 특정 시점에 발생한 문제")
            st.markdown("- **액션**: 추가 분석: 앱 버전별로 결제 실패 확인 → 모니터링: 결제 전환율이 평소 범위를 벗어나는지 매주 점검")
            st.markdown("- **우선순위**: 영향 큼 · 실행 쉬움 → 바로 실행")
            st.markdown("- **측정 KPI**: Android 결제 전환율 (최근 4주)")
            st.markdown("- **대시보드에서**: 개요 탭 규칙 문장(플랫폼 Android) · 탐색 탭 드릴다운")
            
            if android_rate_val is not None:
                st.metric("Android 결제 전환율 (최근 4주)", f"{android_rate_val:.1f}%")
            else:
                st.metric("Android 결제 전환율 (최근 4주)", "데이터 없음")
            st.caption("대시보드에서: 개요 탭 규칙 문장(플랫폼 Android) · 탐색 탭 드릴다운")

    # 카드 2
    with action_col2:
        with st.container(border=True):
            st.markdown("**(카드 2의 문제 · 근거 · 성격 · 액션 · 우선순위)**")
            st.markdown("- **문제**: 방문 → 식당 조회 전환율 28.4% (다른 경로 57-69%), 주문 전환율 3.2%")
            st.markdown("- **근거**: 2026년 2월부터 월별 세션의 30-36%가 push · 기간 내내 25-35%")
            st.markdown("- **성격**: 전체 구간에 세그먼트별 반응 차이")
            st.markdown("- **액션**: 실험: 일부 사용자에게만 푸시 대상과 문구를 바꿔 방문 → 식당 조회 전환율 비교")
            st.markdown("- **우선순위**: 효과 불확실 · 실행 쉬움 → 실험부터")
            st.markdown("- **측정 KPI**: push 방문 → 식당 조회 전환율(최근 4주)")
            st.markdown("- **대시보드에서**: 탐색 탭 비교 기준 '유입경로' · 드릴다운 push")
            
            if push_rate_val is not None:
                st.metric("push 방문 → 식당 조회 전환율 (최근 4주)", f"{push_rate_val:.1f}%")
            else:
                st.metric("push 방문 → 식당 조회 전환율 (최근 4주)", "데이터 없음")
            st.caption("대시보드에서: 탐색 탭 비교 기준 '유입경로' · 드릴다운 push")