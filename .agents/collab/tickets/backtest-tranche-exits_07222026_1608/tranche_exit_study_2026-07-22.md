# Tranche-Exit Decision-Gate Study - 2026-07-22

Gate for live Stage 3 (oco-leg-race-audit_07212026_1935). A ladder counts as a WIN only when it beats the same-lots single-TP baseline on BOTH net PnL and PnL/DD.

## CL lots=2 (baseline net +280847, PnL/DD 7.315)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +280847 | 1.296 | -38393 | 7.315 | 1141 | - |
| L2a_50-50_0.7-1.3 | +298013 | 1.334 | -40738 | 7.315 | 1102 | no |
| L2b_60-40_0.8-1.5 | +285719 | 1.322 | -43037 | 6.639 | 1092 | no |
| L3_40-30-30_0.6-1.0-1.6 | +259183 | 1.273 | -35459 | 7.309 | 1140 | no |

## CL lots=3 (baseline net +421270, PnL/DD 7.364)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +421270 | 1.296 | -57209 | 7.364 | 1141 | - |
| L2a_50-50_0.7-1.3 | +444686 | 1.332 | -58851 | 7.556 | 1102 | YES |
| L2b_60-40_0.8-1.5 | +431179 | 1.324 | -62842 | 6.861 | 1092 | no |
| L3_40-30-30_0.6-1.0-1.6 | +426898 | 1.321 | -59180 | 7.214 | 1091 | no |

## MES lots=2 (baseline net +41161, PnL/DD 5.374)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +41161 | 1.236 | -7659 | 5.374 | 1236 | - |
| L2a_50-50_0.7-1.3 | +23256 | 1.135 | -8336 | 2.79 | 1202 | no |
| L2b_60-40_0.8-1.5 | +18036 | 1.104 | -9810 | 1.838 | 1181 | no |
| L3_40-30-30_0.6-1.0-1.6 | +35033 | 1.201 | -6830 | 5.129 | 1235 | no |

## MES lots=3 (baseline net +61741, PnL/DD 5.388)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +61741 | 1.236 | -11458 | 5.388 | 1236 | - |
| L2a_50-50_0.7-1.3 | +32607 | 1.126 | -12604 | 2.587 | 1202 | no |
| L2b_60-40_0.8-1.5 | +27449 | 1.105 | -14180 | 1.936 | 1181 | no |
| L3_40-30-30_0.6-1.0-1.6 | +23290 | 1.089 | -13532 | 1.721 | 1183 | no |

## MGC lots=2 (baseline net +54276, PnL/DD 5.302)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +54276 | 1.345 | -10237 | 5.302 | 884 | - |
| L2a_50-50_0.7-1.3 | +46593 | 1.304 | -9802 | 4.753 | 857 | no |
| L2b_60-40_0.8-1.5 | +55192 | 1.367 | -9099 | 6.066 | 848 | YES |
| L3_40-30-30_0.6-1.0-1.6 | +43599 | 1.278 | -9851 | 4.426 | 883 | no |

## MGC lots=3 (baseline net +81414, PnL/DD 5.302)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +81414 | 1.345 | -15355 | 5.302 | 884 | - |
| L2a_50-50_0.7-1.3 | +65885 | 1.287 | -14605 | 4.511 | 857 | no |
| L2b_60-40_0.8-1.5 | +79062 | 1.35 | -13627 | 5.802 | 848 | no |
| L3_40-30-30_0.6-1.0-1.6 | +76563 | 1.341 | -13627 | 5.618 | 846 | no |

## NG lots=2 (baseline net +345275, PnL/DD 8.158)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +345275 | 1.332 | -42326 | 8.158 | 1086 | - |
| L2a_50-50_0.7-1.3 | +328694 | 1.32 | -44225 | 7.432 | 1075 | no |
| L2b_60-40_0.8-1.5 | +326371 | 1.314 | -43395 | 7.521 | 1066 | no |
| L3_40-30-30_0.6-1.0-1.6 | +308531 | 1.299 | -39169 | 7.877 | 1085 | no |

## NG lots=3 (baseline net +517912, PnL/DD 8.387)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +517912 | 1.332 | -61754 | 8.387 | 1086 | - |
| L2a_50-50_0.7-1.3 | +471622 | 1.306 | -65614 | 7.188 | 1075 | no |
| L2b_60-40_0.8-1.5 | +471927 | 1.303 | -63484 | 7.434 | 1066 | no |
| L3_40-30-30_0.6-1.0-1.6 | +533632 | 1.358 | -60734 | 8.786 | 1061 | YES |

## SIL lots=2 (baseline net +119848, PnL/DD 2.382)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +119848 | 1.242 | -50304 | 2.382 | 938 | - |
| L2a_50-50_0.7-1.3 | +86438 | 1.177 | -59718 | 1.447 | 919 | no |
| L2b_60-40_0.8-1.5 | +103600 | 1.214 | -59510 | 1.741 | 915 | no |
| L3_40-30-30_0.6-1.0-1.6 | +93997 | 1.192 | -50822 | 1.85 | 934 | no |

## SIL lots=3 (baseline net +179772, PnL/DD 2.425)

| variant | net PnL | PF | max DD | PnL/DD | trades | beats baseline |
|---|---|---|---|---|---|---|
| baseline_singleTP | +179772 | 1.242 | -74131 | 2.425 | 938 | - |
| L2a_50-50_0.7-1.3 | +121111 | 1.165 | -86073 | 1.407 | 919 | no |
| L2b_60-40_0.8-1.5 | +147972 | 1.204 | -86707 | 1.707 | 915 | no |
| L3_40-30-30_0.6-1.0-1.6 | +171089 | 1.243 | -80705 | 2.12 | 908 | no |

## Verdict

- L2a_50-50_0.7-1.3: beat baseline in 1/10 symbol-lot groups
- L2b_60-40_0.8-1.5: beat baseline in 1/10 symbol-lot groups
- L3_40-30-30_0.6-1.0-1.6: beat baseline in 1/10 symbol-lot groups

**NO-GO** - no ladder variant beat the tuned single-TP baseline in a majority of symbol-lot groups; keep single-TP exits and leave live Stage 3 shelved (multi-lot all-in/all-out remains available without Stage 3).
