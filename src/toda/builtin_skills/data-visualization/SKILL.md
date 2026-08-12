---
name: data-visualization
display_name: Data Visualization
description: Turn sourced quantitative data into accurate calculations and SVG charts with Toda's calculate and chart_create tools.
version: "1.0.0"
license: MIT
origin:
  project: toda
  implementation: original
kind: builtin
platform_key: builtin.toda.data_visualization
activation: {mode: optional}
required_capabilities: {all_of: [compute.calculate, chart.create]}
warnings:
  - Never chart a derived number that was computed mentally; calculate it and retain the source values needed to reproduce it.
  - A completed tool call is not proof of a usable chart; retrieve the returned artifact and verify its type, labels, series, and stated simplifications.
  - Do not use a pie chart for negative values, multiple series, or data that is not a single part-to-whole breakdown.
steps:
  - order: 1
    name: Establish the data
    description: Confirm source, grain, units, freshness, and which values are raw versus derived.
  - order: 2
    name: Compute and choose
    description: Calculate derived values and select the smallest chart type that answers the user's question.
  - order: 3
    name: Render and verify
    description: Create the SVG artifact, read it back, and verify the resulting state before delivery.
---
Use this skill when the point is comparison, change over time, relationship,
or part-to-whole—not merely because the answer contains numbers. For a
two-number answer, use `calculate` and explain it directly. For a table the
user needs to edit, use the xlsx skill.

## Establish trustworthy inputs

Record the data source, observation period, unit, and grain before rendering.
Do not mix daily and monthly values, currencies, percentages and percentage
points, or actuals and forecasts in one series without making the distinction
explicit. Carry incomplete, stale, sampled, or filtered data into the title,
labels, or delivery note.

Use `calculate` for every derived value that will appear as fact: totals,
shares, percentage change, and date spans. Retain the original inputs so the
result can be reproduced. Arithmetic correctness does not prove the source
values are correct.

## Choose the chart from the question

- **Bar:** compare categories at one point or period.
- **Line:** show ordered or time-based change; preserve chronological order.
- **Scatter:** show a relationship between numeric variables; provide a
  numeric `x` array matching every series value.
- **Pie:** show one non-negative part-to-whole series with a meaningful total.
  Prefer a bar chart when there are many slices or close values.

`chart_create` supports at most eight series and 200 points per series. If the
source exceeds that, aggregate deliberately and state what was grouped. Never
silently truncate.

## Render and verify

Call `chart_create` with a descriptive title, explicit series names, labels,
and axis units. After it returns:

1. Confirm the artifact exists and is an `image.svg` with `image/svg+xml`.
2. Read it back and confirm it contains a real `<svg>` document.
3. Compare its `chart_type`, series count, and point count with the request.
4. Recheck a representative derived value when the chart is decision-relevant.

Only then attach or share the artifact. Use the normal artifact-sharing tool
when it leaves the current Task so scope authorization is checked at delivery.
