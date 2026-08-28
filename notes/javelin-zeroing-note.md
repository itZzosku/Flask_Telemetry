# Delta Javelin — Scope Zeroing Note

Scope: **Delta Javelin**, shared between three rifles. All values in **MRAD**.

**Reference: T1X = 0 / 0** (lowest elevation of the three). Zero stop is set **0.5 MRAD below** the T1X zero, so the T1X zero sits 0.5 up from the hard stop.

| Rifle | Caliber | Zero distance | Rail / mount |
|---|---|---|---|
| AR-15 | .223 Rem | 100 m | Flat rail, 0 MOA mount |
| T3X Super Varmint | 6.5 Creedmoor | 100 m | Flat rail, 0 MOA mount |
| T1X | .22 LR | 50 m | **20 MOA canted rail** |

---

## Dial positions (primary reference)

When swapping the scope, set the turrets to the target rifle's positions:

| Rifle | Elevation dial | Windage dial |
|---|---|---|
| **T1X** (reference) | **0** | **0** |
| AR-15 | **~ +1.3** *(provisional)* | **~ 3.6 LEFT** *(provisional)* |
| T3X Super Varmint | **+4.5** *(measured)* | **3.7 LEFT** *(measured)* |

*(All positions are up and left of the T1X — everything dials in one direction from the reference, and the zero stop sits safely 0.5 below it.)*

---

## Swap adjustments (derived from dial positions)

| Swap | Elevation | Windage |
|---|---|---|
| T1X → T3X | UP 4.5 | 3.7 LEFT |
| T3X → T1X | DOWN 4.5 | 3.7 RIGHT |
| T1X → AR-15 | UP ~1.3 | ~3.6 LEFT |
| AR-15 → T1X | DOWN ~1.3 | ~3.6 RIGHT |
| AR-15 → T3X | UP ~3.2 | ~0.1 LEFT |
| T3X → AR-15 | DOWN ~3.2 | ~0.1 RIGHT |

*(Rows involving the AR-15 are provisional until it is re-zeroed.)*

---

## Notes

- T1X → T3X was **measured** at UP 4.5 / 3.7 L in the new frame — somewhat less than the UP 5.8 / 4.1 L computed from the old dial readings. Trust the measured values; the difference likely comes from re-zeroing shifts or remount variation. Worth confirming with a check shot after the next couple of swaps until the numbers prove repeatable.
- The measured 4.5 MRAD elevation difference is a bit under the theoretical 5.8 MRAD (20 MOA) from the T1X's canted rail — mount height and trajectory differences (.22 LR at 50 m vs 6.5 CM at 100 m) account for the rest.
- Old dial frame for reference: AR-15 was 0 / 0, T3X +4.5 / 0.5 L, T1X −1.3 / 3.6 R.
- AR-15 position is estimated from the old frame and a zero that was hitting low — treat as rough until re-zeroed.

## To-do

- [x] Zero T1X (2026-08-17)
- [x] Set T1X as 0 / 0 reference, zero stop 0.5 below
- [x] Measure T1X → T3X in new frame: UP 4.5 / 3.7 LEFT
- [ ] Reshoot AR-15 zero → record its dial positions in the new frame and update the table

*Last updated: 2026-08-17 — T1X is the 0/0 reference; T3X measured and confirmed; AR-15 pending re-zero.*
