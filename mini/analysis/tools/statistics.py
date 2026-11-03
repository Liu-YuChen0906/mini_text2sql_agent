"""Pure calculations over actual snapshots; no model-generated rows accepted."""

import math
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("null_or_non_numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non_finite")
    return result


def records(snapshot):
    return [
        dict(zip(snapshot["columns"], row, strict=True)) for row in snapshot["rows"]
    ]


def aggregate(snapshot, metric, dimensions):
    result = defaultdict(float)
    for row in records(snapshot):
        key = tuple(row[k] for k in dimensions)
        result[key] += number(row[metric])
    return dict(result)


def periods(snapshots, p):
    dimensions = p.get("dimensions", [])
    metric = p["metric"]
    if p.get("aggregation", "sum") != "sum":
        raise ValueError("only_additive_sum_supported")
    current, baseline = [aggregate(s, metric, dimensions) for s in snapshots]
    missing = set(current) ^ set(baseline)
    if missing and p.get("missing_groups", "reject") != "zero":
        raise ValueError(
            "missing_groups: choose explicit zero policy or repair coverage"
        )
    rows = []
    warnings = []
    if missing:
        warnings.append("缺失分组按显式策略补零；须人工确认分组完备")
    for key in sorted(set(current) | set(baseline), key=str):
        c, b = current.get(key, 0), baseline.get(key, 0)
        delta = c - b
        rate = None if b == 0 else delta / b
        if b <= 0:
            warnings.append(
                "零基期不可计算相对变化"
                if b == 0
                else "负基期使用原分母，相对变化需谨慎解释"
            )
        rows.append(
            dict(
                group=list(key),
                current=c,
                baseline=b,
                delta=delta,
                change_rate=rate,
                change_percent=None if rate is None else rate * 100,
            )
        )
    c, b = sum(current.values()), sum(baseline.values())
    delta = c - b
    rate = None if b == 0 else delta / b
    return dict(
        groups=rows,
        current=c,
        baseline=b,
        delta=delta,
        change_rate=rate,
        change_percent=None if rate is None else rate * 100,
        rate_reason="zero_baseline"
        if b == 0
        else "negative_baseline"
        if b < 0
        else None,
    ), warnings


def calculate(name, snapshots, p):
    expected = 2 if name in ("compare_periods", "contribution_analysis") else 1
    if len(snapshots) != expected:
        raise ValueError(f"requires_exactly_{expected}_datasets")
    if (
        len(p.get("columns", [])) > 32
        or len(p.get("group_by", [])) > 4
        or len(p.get("dimensions", [])) > 4
    ):
        raise ValueError("analysis_column_budget_exhausted")
    if any(not s["rows"] for s in snapshots):
        raise ValueError("empty_dataset_not_zero")
    warnings = []
    data = records(snapshots[0])
    if name == "inspect_dataset":
        return dict(
            row_count=len(data),
            columns=snapshots[0]["columns"],
            missing={
                k: sum(r[k] is None for r in data) for k in snapshots[0]["columns"]
            },
            duplicate_rows=len(data) - len({repr(r) for r in snapshots[0]["rows"]}),
            preview=snapshots[0]["rows"][:5],
        ), warnings
    if name == "describe_dataset":
        ddof = p.get("ddof", 1)
        if ddof not in (0, 1):
            raise ValueError("ddof_must_be_zero_or_one")
        groups = defaultdict(list)
        for row in data:
            groups[tuple(row[k] for k in p.get("group_by", []))].append(row)
        result = []
        for key, rows in groups.items():
            columns = {}
            for col in p["columns"]:
                values = sorted(number(r[col]) for r in rows if r[col] is not None)
                n = len(values)

                def quantile(q, values=values, n=n):
                    x = (n - 1) * q
                    lo, hi = math.floor(x), math.ceil(x)
                    return values[lo] + (values[hi] - values[lo]) * (x - lo)

                columns[col] = dict(
                    count=n,
                    missing=len(rows) - n,
                    sum=sum(values) if n else None,
                    mean=st.mean(values) if n else None,
                    median=st.median(values) if n else None,
                    min=min(values) if n else None,
                    max=max(values) if n else None,
                    std=(st.stdev(values) if ddof else st.pstdev(values))
                    if n > ddof
                    else None,
                    q25=quantile(0.25) if n else None,
                    q75=quantile(0.75) if n else None,
                )
            result.append(dict(group=list(key), columns=columns))
        return dict(groups=result, ddof=ddof, quantile_method="linear"), warnings
    if name in ("compare_periods", "contribution_analysis"):
        if len(snapshots) != 2:
            raise ValueError("requires_current_then_baseline")
        result, warnings = periods(snapshots, p)
        if name == "contribution_analysis":
            if len(p.get("dimensions", [])) != 1 or p.get("additive") is not True:
                raise ValueError(
                    "contribution_requires_one_dimension_and_additive_metric"
                )
            for row in result["groups"]:
                row["contribution"] = (
                    row["delta"] / result["delta"] if result["delta"] else None
                )
            result["reconciliation_error"] = (
                sum(r["delta"] for r in result["groups"]) - result["delta"]
            )
            result["contribution_sum"] = (
                sum(r["contribution"] for r in result["groups"])
                if result["delta"]
                else None
            )
            if not result["delta"]:
                warnings.append("总变化为零，贡献度不可计算")
        return result, warnings
    if name in ("analyze_trend", "detect_anomalies"):
        if p.get("granularity", "day") != "day":
            raise ValueError("only_daily_granularity_supported")
        dates = defaultdict(list)
        for row in data:
            stamp = datetime.fromisoformat(str(row[p["time_column"]]))
            if stamp.tzinfo is not None:
                from zoneinfo import ZoneInfo

                stamp = stamp.astimezone(ZoneInfo(p.get("timezone", "Asia/Shanghai")))
            dates[stamp.date().isoformat()].append(number(row[p["metric"]]))
        if (
            any(len(v) > 1 for v in dates.values())
            and p.get("duplicate_dates", "reject") != "sum"
        ):
            raise ValueError("duplicate_dates: explicit sum required")
        sequence = [(k, sum(v)) for k, v in sorted(dates.items())]
        missing = []
        start, end = (datetime.fromisoformat(sequence[i][0]) for i in (0, -1))
        while start <= end:
            if start.date().isoformat() not in dates:
                missing.append(start.date().isoformat())
            if len(missing) > 10000:
                raise ValueError("date_range_too_large")
            start += timedelta(days=1)
        if missing:
            warnings.append("缺失日期未补值；窗口按实际观测点计算")
        window = int(p.get("window", 7))
        if window < 2:
            raise ValueError("window_at_least_two")
        rows = []
        for i, (day, value) in enumerate(sequence):
            history = [v for _, v in sequence[max(0, i - window) : i]]
            previous = sequence[i - 1][1] if i else None
            row = dict(
                day=day,
                value=value,
                change_rate=(value - previous) / previous if previous else None,
            )
            if name == "analyze_trend":
                row["moving_mean"] = st.mean(
                    [v for _, v in sequence[max(0, i - window + 1) : i + 1]]
                )
            else:
                method, threshold = (
                    p.get("method", "zscore"),
                    number(p.get("threshold", 3)),
                )
                if method != "zscore" or threshold <= 0:
                    raise ValueError(
                        "only_positive_threshold_historical_zscore_supported"
                    )
                base = st.mean(history) if len(history) == window else None
                std = st.stdev(history) if base is not None else None
                score = (value - base) / std if std else None
                row.update(
                    baseline=base,
                    score=score,
                    threshold=threshold,
                    anomaly=abs(score) > threshold if score is not None else None,
                    reason="insufficient_history"
                    if base is None
                    else "zero_std"
                    if not std
                    else None,
                )
            rows.append(row)
        return dict(
            series=rows,
            missing_dates=missing,
            window=window,
            window_basis="observations",
        ), warnings
    if name == "correlation_analysis":
        cols = p["columns"]
        keys = p.get("alignment_keys", [])
        if not keys:
            raise ValueError("alignment_keys_required")
        if len({tuple(r[k] for k in keys) for r in data}) != len(data):
            raise ValueError("duplicate_alignment_keys")
        if len(cols) < 2:
            raise ValueError("at_least_two_columns")
        method = p.get("method", "pearson")
        if method not in ("pearson", "spearman"):
            raise ValueError("unknown_correlation_method")
        if p.get("missing", "pairwise_drop") != "pairwise_drop":
            raise ValueError("only_pairwise_drop_supported")
        minimum = max(3, int(p.get("min_samples", 3)))

        def rank(values):
            ordered = sorted(values)
            return [
                sum(i + 1 for i, v in enumerate(ordered) if v == x) / ordered.count(x)
                for x in values
            ]

        pairs = []
        for i, a in enumerate(cols):
            for b in cols[i + 1 :]:
                aligned = [
                    (number(r[a]), number(r[b]))
                    for r in data
                    if r[a] is not None and r[b] is not None
                ]
                reason, coeff = None, None
                if len(aligned) < minimum:
                    reason = "insufficient_samples"
                else:
                    x, y = map(list, zip(*aligned))
                    if method == "spearman":
                        x, y = rank(x), rank(y)
                    if len(set(x)) == 1 or len(set(y)) == 1:
                        reason = "constant_column"
                    else:
                        coeff = st.correlation(x, y)
                pairs.append(
                    dict(
                        columns=[a, b],
                        coefficient=coeff,
                        sample_count=len(aligned),
                        reason=reason,
                    )
                )
        return dict(
            pairs=pairs, method=method, missing="pairwise_drop", min_samples=minimum
        ), ["描述性相关不能证明因果"]
    raise ValueError("unknown_analysis_tool")


NAMES = (
    "inspect_dataset",
    "describe_dataset",
    "compare_periods",
    "analyze_trend",
    "contribution_analysis",
    "correlation_analysis",
    "detect_anomalies",
)
