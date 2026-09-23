"""Chronic Care Market Prioritization Explorer -- Streamlit application.

Audience: commercial strategy and population-health leads at a digital
chronic-care company. The question the app answers is deliberately narrow:

    "Which US states and which populations should we target next,
     and where are diagnosed patients falling out of care?"

Run with:  streamlit run app.py   (after `python etl.py`)
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src import analytics, config, storage

st.set_page_config(
    page_title="Chronic Care Market Prioritization Explorer",
    page_icon="🩺",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#1f77b4"
SEQ = "Blues"
STATUS_COLOR = {"PASS": "#1a7f37", "FAIL": "#b42318"}
SEVERITY_ICON = {"ERROR": "🔴", "WARNING": "🟠", "INFO": "⚪"}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Loading analytical tables ...")
def load_all() -> dict[str, pd.DataFrame]:
    names = [
        "mart_indicator_state_year", "mart_pillar_burden", "mart_equity_gap",
        "mart_state_scorecard", "dq_results", "dim_indicator", "dim_location",
        "fact_indicator",
    ]
    return {n: storage.read_table(n) for n in names}


@st.cache_data
def load_meta() -> tuple[dict | None, dict | None]:
    return (
        storage.read_json(config.RUN_MANIFEST_PATH),
        storage.read_json(config.DQ_SUMMARY_PATH),
    )


def require_data() -> dict[str, pd.DataFrame]:
    try:
        return load_all()
    except FileNotFoundError:
        st.error("**No processed data found.** The ETL has not been run yet.")
        st.code("python etl.py", language="bash")
        st.caption(
            "No network? `python etl.py --offline` rebuilds from the cached raw "
            "snapshot in `data/raw/`."
        )
        st.stop()


def fmt_pct(v: float | None, dp: int = 1) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:.{dp}f}%"


def fmt_people(v: float | None) -> str:
    """Headcounts are read at a glance, so scale them rather than print 8 digits."""
    if v is None or pd.isna(v):
        return "n/a"
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:,.0f}"


def download(df: pd.DataFrame, label: str, name: str) -> None:
    st.download_button(
        label, df.to_csv(index=False).encode("utf-8"), file_name=name,
        mime="text/csv", width="content",
    )


data = require_data()
manifest, dq_summary = load_meta()

indicator_mart = data["mart_indicator_state_year"]
pillar_burden = data["mart_pillar_burden"]
equity_gap = data["mart_equity_gap"]
scorecard_base = data["mart_state_scorecard"]
dq_results = data["dq_results"]
dim_indicator = data["dim_indicator"]

LATEST_YEAR = int(indicator_mart["year"].max())
YEARS = sorted(indicator_mart["year"].unique().tolist())
PILLARS = sorted(dim_indicator["pillar"].unique().tolist())


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🩺 Chronic Care Explorer")
    st.caption("CDC Chronic Disease Indicators · BRFSS")

    year = st.selectbox("Survey year", YEARS, index=len(YEARS) - 1)
    pillars = st.multiselect("Condition pillars", PILLARS, default=PILLARS)
    region_options = sorted(indicator_mart["census_region"].dropna().unique().tolist())
    regions = st.multiselect("Census region", region_options, default=region_options)

    st.divider()
    st.markdown("**Ranking basis**")
    basis = st.radio(
        "Rank states by",
        config.RANKING_BASES,
        index=config.RANKING_BASES.index(config.DEFAULT_RANKING_BASIS),
        format_func=lambda b: {
            "rate": "Rate: where need is concentrated",
            "lives": "Lives: where the people are",
        }[b],
        label_visibility="collapsed",
    )
    st.caption(
        "**Rate** ranks on the Opportunity Score alone, so small states can top "
        "the list. **Lives** weights that score by the number of condition-cases "
        "in the state. Both are defensible; they disagree sharply, and the "
        "disagreement is shown below the ranking."
    )

    st.divider()
    st.markdown("**Opportunity Score weights**")
    st.caption(
        "The score is a weighted blend of four percentile-ranked components. "
        "Move the sliders to stress-test the ranking against a different "
        "commercial thesis."
    )
    w = {
        "burden": st.slider("Disease burden", 0.0, 1.0, config.SCORE_WEIGHTS["burden"], 0.05),
        "trend": st.slider("Worsening trend", 0.0, 1.0, config.SCORE_WEIGHTS["trend"], 0.05),
        "equity_gap": st.slider("Equity gap", 0.0, 1.0, config.SCORE_WEIGHTS["equity_gap"], 0.05),
        "care_gap": st.slider("Untreated care gap", 0.0, 1.0, config.SCORE_WEIGHTS["care_gap"], 0.05),
    }
    if sum(w.values()) == 0:
        st.warning("At least one weight must be above zero; reverting to defaults.")
        w = dict(config.SCORE_WEIGHTS)

    st.divider()
    if dq_summary:
        badge = {"PASS": "🟢 PASS", "WARN": "🟠 WARN", "FAIL": "🔴 FAIL"}[
            dq_summary["overall_status"]
        ]
        st.markdown(f"**Data quality:** {badge}")
        st.caption(f"{dq_summary['passed']}/{dq_summary['checks_run']} checks passed")
    if manifest:
        st.caption(f"ETL run: {manifest['run_completed_at']}")

scorecard = analytics.rescore(scorecard_base, w, basis)

# The column the ranking is actually sorted on, plus how to present it.
SCORE_COL = "opportunity_score" if basis == "rate" else "opportunity_adults"
SCORE_LABEL = "Opportunity Score" if basis == "rate" else "Opportunity-weighted caseload"

# Filtered views used across tabs.
ind_f = indicator_mart[
    indicator_mart["pillar"].isin(pillars)
    & indicator_mart["is_addressable_market"].fillna(False)
    & indicator_mart["census_region"].isin(regions)
]
score_f = scorecard[scorecard["census_region"].isin(regions)] if not scorecard.empty else scorecard


st.title("Chronic Care Market Prioritization Explorer")
st.caption(
    "Where should a digital chronic-care company deploy next? This app ranks US "
    "states on disease burden, trajectory, health equity gaps and untreated "
    "patients across diabetes, cardiovascular, weight and behavioral health."
)

tab_market, tab_state, tab_equity, tab_dq, tab_about = st.tabs(
    ["🎯 Market Opportunity", "📍 State Deep Dive", "⚖️ Equity & Access",
     "🔍 Data Quality", "ℹ️ Method & Data Model"]
)


# ---------------------------------------------------------------------------
# Tab 1 -- Market opportunity
# ---------------------------------------------------------------------------
with tab_market:
    if score_f.empty:
        st.info("No states match the current filters.")
    else:
        top = score_f.iloc[0]
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("States ranked", len(score_f), help=f"Scored on {LATEST_YEAR} data")
        c2.metric(f"Top target · by {basis}", f"{top['location_name']}",
                  f"score {top['opportunity_score']:.1f}")
        c3.metric("Condition caseload", fmt_people(score_f["condition_caseload"].sum()),
                  help="Estimated condition-cases across the states in view: each "
                       "prevalence rate multiplied by that state's adult population, "
                       "summed over the tracked conditions. Counts cases, not unique "
                       "people. An adult with two conditions is counted twice.")
        c4.metric("Untreated adults", fmt_people(score_f["untreated_adults"].sum()),
                  help="Adults with high blood pressure or high cholesterol who "
                       "report not taking medication for it. The directly "
                       "addressable population for a medication-adherence programme.")
        c5.metric("Median race gap", f"{score_f['mean_race_gap_pp'].median():.1f} pp",
                  help="Average within-state spread between the highest and lowest "
                       "race/ethnicity group across burden indicators.")

        st.markdown(f"#### {SCORE_LABEL} by state")
        map_col, bar_col = st.columns([1.35, 1])
        with map_col:
            fig = px.choropleth(
                score_f, locations="location_abbr", locationmode="USA-states",
                color=SCORE_COL, scope="usa",
                color_continuous_scale=SEQ,
                range_color=(0, 100) if basis == "rate" else None,
                hover_name="location_name",
                hover_data={
                    "opportunity_score": ":.1f", "burden_index": ":.1f",
                    "untreated_pct": ":.1f", "mean_race_gap_pp": ":.1f",
                    "condition_caseload": ":,.0f",
                    "location_abbr": False,
                },
                labels={"opportunity_score": "Opportunity",
                        "opportunity_adults": "Opportunity-weighted caseload",
                        "condition_caseload": "Condition caseload"},
            )
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=430,
                              coloraxis_colorbar=dict(
                                  title="Score" if basis == "rate" else "Cases"))
            st.plotly_chart(fig, width="stretch")

        with bar_col:
            top10 = score_f.head(10).sort_values(SCORE_COL)
            fig = px.bar(
                top10, x=SCORE_COL, y="location_name", orientation="h",
                color=SCORE_COL, color_continuous_scale=SEQ,
                range_color=(0, 100) if basis == "rate" else None,
                text=SCORE_COL,
            )
            if basis == "rate":
                fig.update_traces(texttemplate="%{text:.0f}", textposition="outside")
                xaxis_range = [0, 108]
            else:
                fig.update_traces(texttemplate="%{text:.3s}", textposition="outside")
                xaxis_range = [0, float(top10[SCORE_COL].max()) * 1.18]
            fig.update_layout(
                margin=dict(l=0, r=10, t=10, b=0), height=430, showlegend=False,
                coloraxis_showscale=False, xaxis_title=SCORE_LABEL,
                yaxis_title="", xaxis_range=xaxis_range,
            )
            st.plotly_chart(fig, width="stretch")

        st.markdown("#### What is driving the ranking?")
        st.caption(
            "Each component is a 0-100 percentile rank across states, so they are "
            "directly comparable. Higher always means *more* commercial opportunity."
        )
        drivers = score_f.head(10)[
            ["location_name", "burden_component", "trend_component",
             "equity_component", "care_gap_component"]
        ].set_index("location_name")
        drivers.columns = ["Burden", "Worsening trend", "Equity gap", "Untreated"]
        fig = px.imshow(
            drivers.T, color_continuous_scale=SEQ, aspect="auto",
            labels=dict(color="Percentile"), text_auto=".0f", zmin=0, zmax=100,
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=280,
                          xaxis_title="", yaxis_title="")
        st.plotly_chart(fig, width="stretch")

        st.markdown("#### Concentration vs scale")
        st.caption(
            "Rate and lives are both defensible readings of the same data, and "
            "they disagree. A state high on both axes is rare and is the only "
            "kind of market that survives either thesis."
        )
        sc = score_f.dropna(subset=["condition_caseload"]).copy()
        if sc.empty:
            st.info("No population denominator available for the states in view.")
        else:
            fig = px.scatter(
                sc, x="condition_caseload", y="opportunity_score",
                size="untreated_adults", color="census_region",
                hover_name="location_name", log_x=True, size_max=38,
                labels={
                    "condition_caseload": "Condition caseload (log scale)",
                    "opportunity_score": "Opportunity Score",
                    "census_region": "Region",
                },
                text="location_abbr",
            )
            fig.update_traces(textposition="top center",
                              textfont=dict(size=9, color="#555"))
            fig.add_hline(y=float(sc["opportunity_score"].median()),
                          line_dash="dot", line_color="#999")
            fig.add_vline(x=float(sc["condition_caseload"].median()),
                          line_dash="dot", line_color="#999")
            fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), height=430,
                              legend_title_text="Region")
            st.plotly_chart(fig, width="stretch")

            movers = sc.reindex(
                sc["rank_shift"].abs().sort_values(ascending=False).index
            ).head(6)
            mv = movers[["location_name", "rank_rate", "rank_lives", "rank_shift",
                         "condition_caseload"]].copy()
            mv["condition_caseload"] = mv["condition_caseload"].map(fmt_people)
            mv.columns = ["State", "Rank by rate", "Rank by lives",
                          "Shift", "Caseload"]
            st.caption("**Where the two lenses disagree most**")
            st.dataframe(mv, width="stretch", hide_index=True)
            st.caption(
                "A positive shift means the state ranks better on volume than on "
                "rate, a big population diluting an unremarkable rate. A negative "
                "shift is the reverse: genuinely concentrated need, but not many "
                "people. Neither is wrong; they answer different questions."
            )

        with st.expander("Full scorecard table"):
            show = score_f[[
                "rank", "location_abbr", "location_name", "census_region",
                "opportunity_score", "adult_population", "condition_caseload",
                "untreated_adults", "rank_rate", "rank_lives",
                "burden_index", "mean_prevalence",
                "prevalence_trend_pp_per_year", "mean_race_gap_pp", "untreated_pct",
            ]].rename(columns={
                "rank": "Rank", "location_abbr": "ST", "location_name": "State",
                "census_region": "Region", "opportunity_score": "Opportunity",
                "adult_population": "Adults 18+", "condition_caseload": "Caseload",
                "untreated_adults": "Untreated adults",
                "rank_rate": "Rank (rate)", "rank_lives": "Rank (lives)",
                "burden_index": "Burden index", "mean_prevalence": "Mean prevalence %",
                "prevalence_trend_pp_per_year": "Trend pp/yr",
                "mean_race_gap_pp": "Race gap pp", "untreated_pct": "Untreated %",
            })
            st.dataframe(show, width="stretch", hide_index=True,
                         height=420)
            download(score_f, "Download scorecard (CSV)", "state_scorecard.csv")

        excluded = (manifest or {}).get("analytics", {}).get(
            "states_excluded_from_scorecard", []
        )
        if excluded:
            st.warning(
                f"**{', '.join(excluded)}** published no {LATEST_YEAR} estimates for "
                "these indicators and are excluded from the ranking. BRFSS "
                "participation is voluntary per state and per module, so this is a "
                "gap in the source, not in the pipeline. See the Data Quality tab."
            )


# ---------------------------------------------------------------------------
# Tab 2 -- State deep dive
# ---------------------------------------------------------------------------
with tab_state:
    states = sorted(ind_f["location_name"].dropna().unique().tolist())
    if not states:
        st.info("No states match the current filters.")
    else:
        default_ix = states.index(score_f.iloc[0]["location_name"]) if (
            not score_f.empty and score_f.iloc[0]["location_name"] in states
        ) else 0
        state = st.selectbox("State", states, index=default_ix)
        sdf = ind_f[ind_f["location_name"] == state]
        row = score_f[score_f["location_name"] == state]

        c1, c2, c3, c4 = st.columns(4)
        if not row.empty:
            r = row.iloc[0]
            c1.metric("Opportunity rank", f"#{int(r['rank'])}", f"of {len(score_f)}")
            c2.metric("Burden index", f"{r['burden_index']:.1f}",
                      help="0-100, percentile across states")
            c3.metric("Untreated rate", fmt_pct(r["untreated_pct"]))
            c4.metric("Race gap", f"{r['mean_race_gap_pp']:.1f} pp")
        else:
            c1.info(f"{state} is not in the {LATEST_YEAR} ranking (no data that year).")

        st.markdown(f"#### {state} vs national benchmark, {year}")
        year_view = sdf[sdf["year"] == year]
        if year_view.empty:
            st.info(f"No {year} estimates published for {state}.")
        else:
            comp = year_view[[
                "short_label", "pillar", "value", "national_value",
                "delta_vs_national", "ci_low", "ci_high", "measure_type",
                "is_low_precision",
            ]].sort_values("delta_vs_national", ascending=False)

            fig = go.Figure()
            fig.add_bar(
                y=comp["short_label"], x=comp["delta_vs_national"], orientation="h",
                marker_color=["#b42318" if d > 0 else "#1a7f37"
                              for d in comp["delta_vs_national"]],
                hovertemplate="%{y}<br>%{x:+.1f} pp vs national<extra></extra>",
            )
            fig.add_vline(x=0, line_width=1, line_color="#666")
            fig.update_layout(
                height=360, margin=dict(l=0, r=0, t=10, b=0),
                xaxis_title="Percentage points vs national benchmark", yaxis_title="",
            )
            st.plotly_chart(fig, width="stretch")
            st.caption(
                "Red = worse than the national benchmark. For the two medication "
                "indicators a *higher* value is good, so a red bar there means more "
                "people are on treatment than average."
            )

            show = comp.rename(columns={
                "short_label": "Indicator", "pillar": "Pillar", "value": "State %",
                "national_value": "National %", "delta_vs_national": "Delta pp",
                "ci_low": "CI low", "ci_high": "CI high", "measure_type": "Measure",
                "is_low_precision": "Wide CI",
            })
            st.dataframe(show, width="stretch", hide_index=True)

        st.markdown("#### Trend with 95% confidence interval")
        pick = st.selectbox(
            "Indicator", sorted(sdf["short_label"].unique().tolist()), key="trend_ind"
        )
        tdf = sdf[sdf["short_label"] == pick].sort_values("year")
        if len(tdf) < 2:
            st.info("Not enough years of data to plot a trend for this indicator.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(tdf["year"]) + list(tdf["year"])[::-1],
                y=list(tdf["ci_high"]) + list(tdf["ci_low"])[::-1],
                fill="toself", fillcolor="rgba(31,119,180,0.15)",
                line=dict(color="rgba(0,0,0,0)"), hoverinfo="skip",
                name="95% CI", showlegend=True,
            ))
            fig.add_trace(go.Scatter(
                x=tdf["year"], y=tdf["value"], mode="lines+markers",
                line=dict(color=ACCENT, width=3), name=state,
            ))
            fig.add_trace(go.Scatter(
                x=tdf["year"], y=tdf["national_value"], mode="lines",
                line=dict(color="#888", width=2, dash="dash"), name="National",
            ))
            fig.update_layout(
                height=380, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_title="Prevalence (%)", xaxis_title="Survey year",
                xaxis=dict(tickmode="array", tickvals=sorted(tdf["year"].unique())),
                legend=dict(orientation="h", y=1.1),
            )
            st.plotly_chart(fig, width="stretch")
            st.caption(
                "The shaded band is CDC's published 95% confidence interval. Where it "
                "is wide the estimate rests on a small sample, so read movement inside "
                "the band as noise, not signal."
            )


# ---------------------------------------------------------------------------
# Tab 3 -- Equity & access
# ---------------------------------------------------------------------------
with tab_equity:
    st.markdown("#### Who carries the burden within a state?")
    st.caption(
        "A state can sit on the national average overall while one demographic "
        "group runs 15+ points above another. Those concentrated pockets of unmet "
        "need are usually the most addressable, and the most fundable."
    )

    eq = equity_gap[
        equity_gap["is_addressable_market"].fillna(False)
        & equity_gap["year"].eq(year)
        & equity_gap["pillar"].isin(pillars)
        & equity_gap["census_region"].isin(regions)
    ]
    if eq.empty:
        st.info(f"No stratified estimates available for {year} under these filters.")
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            category = st.radio(
                "Break down by",
                sorted(eq["stratum_category"].unique().tolist()),
                horizontal=False,
            )
        with c2:
            ind_choice = st.selectbox(
                "Indicator", sorted(eq["short_label"].unique().tolist()), key="eq_ind"
            )

        sub = eq[(eq["stratum_category"] == category) & (eq["short_label"] == ind_choice)]
        if sub.empty:
            st.info("No data for that combination.")
        else:
            top_gap = sub.nlargest(15, "gap_pp").sort_values("gap_pp")
            fig = px.bar(
                top_gap, x="gap_pp", y="location_name", orientation="h",
                color="gap_pp", color_continuous_scale="Reds",
                hover_data={"min_value": ":.1f", "max_value": ":.1f",
                            "most_burdened_group": True, "location_name": False},
                labels={"gap_pp": "Gap (pp)"},
            )
            fig.update_layout(
                height=470, margin=dict(l=0, r=0, t=10, b=0),
                coloraxis_showscale=False, yaxis_title="",
                xaxis_title=f"{category} gap in {ind_choice} (percentage points)",
            )
            st.plotly_chart(fig, width="stretch")

            worst = sub.nlargest(1, "gap_pp").iloc[0]
            st.info(
                f"**Widest {category.lower()} gap:** {worst['location_name']}: "
                f"{worst['gap_pp']:.1f} pp between {worst['lowest_group']} "
                f"({worst['min_value']:.1f}%) and {worst['highest_group']} "
                f"({worst['max_value']:.1f}%). Most burdened group: "
                f"**{worst['most_burdened_group']}**."
            )

            with st.expander("Gap detail table"):
                st.dataframe(
                    sub[["location_abbr", "location_name", "n_strata", "min_value",
                         "max_value", "gap_pp", "gap_ratio", "most_burdened_group"]]
                    .sort_values("gap_pp", ascending=False),
                    width="stretch", hide_index=True,
                )
                download(sub, "Download gap detail (CSV)", "equity_gaps.csv")

    st.divider()
    st.markdown("#### Care gap: burden vs treatment")
    st.caption(
        "The most actionable market is the bottom-right quadrant: high disease "
        "burden combined with a high share of diagnosed patients not on medication."
    )
    if score_f.empty:
        st.info("No states match the current filters.")
    else:
        fig = px.scatter(
            score_f, x="burden_index", y="untreated_pct", text="location_abbr",
            size="opportunity_score", color="census_region",
            hover_name="location_name",
            labels={"burden_index": "Burden index (0-100)",
                    "untreated_pct": "Diagnosed but untreated (%)",
                    "census_region": "Region"},
        )
        fig.update_traces(textposition="top center", textfont_size=9)
        fig.add_hline(y=score_f["untreated_pct"].median(), line_dash="dot",
                      line_color="#999", annotation_text="median untreated")
        fig.add_vline(x=score_f["burden_index"].median(), line_dash="dot",
                      line_color="#999", annotation_text="median burden")
        fig.update_layout(height=520, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")


# ---------------------------------------------------------------------------
# Tab 4 -- Data quality
# ---------------------------------------------------------------------------
with tab_dq:
    st.markdown("#### Pipeline data quality")
    if dq_summary:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Checks run", dq_summary["checks_run"])
        c2.metric("Passed", dq_summary["passed"])
        c3.metric("Failed", dq_summary["failed"])
        c4.metric("Overall", dq_summary["overall_status"])
        if dq_summary["blocking_failures"]:
            st.error(
                "Blocking failures: " + ", ".join(dq_summary["blocking_failures"])
                + ". The ETL exits non-zero so a scheduler can alert on it."
            )

    view = dq_results.copy()
    view["sev"] = view["severity"].map(SEVERITY_ICON) + " " + view["severity"]
    view["result"] = view["status"].map({"PASS": "✅ PASS", "FAIL": "❌ FAIL"})
    st.dataframe(
        view[["check_id", "name", "dimension", "sev", "result", "rows_checked",
              "rows_failed", "fail_rate", "message"]]
        .rename(columns={
            "check_id": "ID", "name": "Check", "dimension": "Dimension",
            "sev": "Severity", "result": "Result", "rows_checked": "Rows",
            "rows_failed": "Failed", "fail_rate": "Fail rate", "message": "Detail",
        }),
        width="stretch", hide_index=True, height=520,
    )

    st.markdown("#### Suppression: what CDC withholds, and where")
    st.caption(
        "CDC blanks any estimate whose sample is too small to be reliable. It is "
        "expected behaviour, but it is not uniform, and suppression clusters in small "
        "states and in smaller race/ethnicity groups, which is precisely where the "
        "equity analysis needs data. Knowing the shape of the hole matters."
    )
    fact = data["fact_indicator"]
    supp = (
        fact.merge(dim_indicator[["indicator_id", "short_label"]], on="indicator_id")
        .groupby(["short_label", "year"])["is_suppressed"].mean()
        .reset_index()
    )
    pivot = supp.pivot(index="short_label", columns="year", values="is_suppressed") * 100
    fig = px.imshow(
        pivot, color_continuous_scale="OrRd", aspect="auto", text_auto=".0f",
        labels=dict(color="% suppressed"), zmin=0, zmax=100,
    )
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                      xaxis_title="Survey year", yaxis_title="")
    st.plotly_chart(fig, width="stretch")

    st.markdown("#### State coverage per indicator-year")
    cov = (
        indicator_mart[indicator_mart["is_addressable_market"].fillna(False)]
        .groupby(["short_label", "year"])["location_id"].nunique()
        .reset_index(name="states")
    )
    cpivot = cov.pivot(index="short_label", columns="year", values="states")
    fig = px.imshow(
        cpivot, color_continuous_scale="Greens", aspect="auto", text_auto=".0f",
        labels=dict(color="States"), zmin=0, zmax=config.N_ADDRESSABLE_STATES,
    )
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                      xaxis_title="Survey year", yaxis_title="")
    st.plotly_chart(fig, width="stretch")
    st.caption(
        f"Out of {config.N_ADDRESSABLE_STATES} addressable geographies (50 states + DC). "
        "BRFSS modules are optional for states in a given year, so dips are a source "
        "characteristic rather than an extraction failure."
    )

    if dq_summary:
        with st.expander("Failing-row samples captured by each check"):
            for d in dq_summary["details"]:
                if d["status"] == "FAIL" and d.get("sample"):
                    st.markdown(f"**{d['check_id']}: {d['name']}**")
                    st.dataframe(pd.DataFrame(d["sample"]),
                                 width="stretch", hide_index=True)

    if manifest:
        with st.expander("ETL run manifest (lineage)"):
            st.json(manifest)


# ---------------------------------------------------------------------------
# Tab 5 -- Method & data model
# ---------------------------------------------------------------------------
with tab_about:
    st.markdown(
        """
#### The product question

A digital chronic-care company sells condition-management programmes to health
plans and employers. Sales capacity is finite, so the commercial question is
**which states to enter next, and which populations to design for**.

Raw disease prevalence alone is a poor answer: it favours large, already
saturated markets and says nothing about whether the problem is growing or who
is being missed. This app blends four signals instead.

#### Opportunity Score

Each component is percentile-ranked 0-100 across states, then blended. Ranking
rather than raw values keeps indicators with different natural scales
(diabetes ~11%, obesity ~35%) from dominating one another.

| Component | Default weight | Reads as |
|---|---|---|
| **Disease burden** | 45% | How much chronic disease exists today |
| **Worsening trend** | 20% | Slope of the burden index over the recent window |
| **Equity gap** | 20% | Mean within-state spread across race/ethnicity groups |
| **Untreated care gap** | 15% | Diagnosed patients reporting no medication |

Weights are adjustable in the sidebar. The components are persisted, so
re-weighting reshuffles the ranking without re-running the ETL. Where a state
is missing a component, the remaining weights are re-normalised so it is not
silently penalised.

#### Market sizing: rate vs lives

A score built from rates answers *where need is most concentrated*, and it
systematically favours small states. Multiplying those rates by each state's
adult population answers a different, equally valid question: *where are the
most affected people*. The sidebar switches between the two.

| Basis | Ranks on | Favours |
|---|---|---|
| **Rate** | Opportunity Score | Small states, the efficiency view |
| **Lives** | Opportunity Score x condition caseload | Large states, the volume view |

**Condition caseload** is the sum over burden indicators of
(prevalence % x adult population). It counts **condition-cases, not unique
people**: an adult with both diabetes and hypertension is counted twice. That
is the right unit for programmes sold per condition, but it is not a patient
count, and BRFSS publishes no comorbidity cross-tabs from which one could be
derived.

The denominator is the **civilian population aged 18+**, because BRFSS only
interviews adults. Using total population would inflate every headcount by
roughly the 22% of Americans never eligible to be surveyed.

#### Data model

A star schema, built because the source is a tall "one row per estimate" feed
with heavy redundancy:

```
fact_indicator  (grain: year x location x indicator x measure_type x stratum)
   |-- dim_location    location_id -> state/territory/national, region, lat/lon
   |-- dim_indicator   indicator_id -> pillar, polarity, measure role
   `-- dim_stratum     stratum_key  -> Overall / Race / Sex / Age
```

Four marts sit on top: `mart_indicator_state_year`, `mart_pillar_burden`,
`mart_equity_gap`, `mart_state_scorecard`. Everything is written as Parquet
*and* loaded into a DuckDB file, so the tables can be read with pandas alone or
queried with SQL.

#### Methodology notes and honest limitations

- **Age-adjusted where possible.** State age structures differ enough that
  crude prevalence misleads on cross-state comparison. Crude is the documented
  fallback and the measure used is recorded on every row.
- **Survey data, not claims.** BRFSS is self-reported and telephone-based. It
  measures *reported* prevalence, which understates undiagnosed disease.
- **Suppression is not random.** It concentrates in small states and small
  demographic groups, which biases equity gaps toward the groups large enough
  to measure. The Data Quality tab shows exactly where the holes are.
- **Caseload counts cases, not people.** See the market-sizing note above; the
  figure must never be read as a count of distinct patients.
- **Correlation, not attribution.** A high score signals unmet need, not proven
  programme ROI.
        """
    )

    pop_meta = (manifest or {}).get("analytics", {})
    if pop_meta:
        st.caption(
            f"Population denominator: {pop_meta.get('population_source', 'n/a')}, "
            f"{pop_meta.get('population_year', 'n/a')} vintage."
        )
        if pop_meta.get("population_year_matches_scoring_year") is False:
            st.warning(
                f"The ranking scores **{LATEST_YEAR}** survey data against a "
                f"**{pop_meta.get('population_year')}** population denominator. "
                "Headcounts are therefore approximate. Re-run "
                "`python scripts/refresh_population.py` against a matching "
                "vintage to close the gap."
            )

    st.markdown("#### Indicators in scope")
    st.dataframe(
        dim_indicator[["indicator_id", "pillar", "short_label", "question",
                       "polarity", "measure_role"]]
        .rename(columns={
            "indicator_id": "CDC ID", "pillar": "Pillar", "short_label": "Label",
            "question": "CDC question", "polarity": "Direction",
            "measure_role": "Role in score",
        }),
        width="stretch", hide_index=True,
    )

    st.caption(
        "Source: CDC Chronic Disease Indicators (dataset `hksd-2xuw`), public "
        "domain, retrieved without authentication from data.cdc.gov."
    )
