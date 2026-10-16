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

| Rifle / load | Elevation dial | Windage dial |
|---|---|---|
| **T1X** (reference) | **0** | **0** |
| AR-15 — GGG 55 gr | **+1.8** | **3.6 LEFT** |
| AR-15 — 69 gr Scenar handload | **+1.6** | **3.2 LEFT** |
| T3X Super Varmint | **+4.5** | **3.7 LEFT** |

*(All positions are up and left of the T1X — everything dials in one direction from the reference, and the zero stop sits safely 0.5 below it.)*

---

## Swap adjustments (derived from dial positions)

| Swap | Elevation | Windage |
|---|---|---|
| T1X → T3X | UP 4.5 | 3.7 LEFT |
| T3X → T1X | DOWN 4.5 | 3.7 RIGHT |
| T1X → AR-15 (55 gr) | UP 1.8 | 3.6 LEFT |
| AR-15 (55 gr) → T1X | DOWN 1.8 | 3.6 RIGHT |
| T1X → AR-15 (69 gr) | UP 1.6 | 3.2 LEFT |
| AR-15 (69 gr) → T1X | DOWN 1.6 | 3.2 RIGHT |
| T3X → AR-15 (55 gr) | DOWN 2.7 | 0.1 RIGHT |
| AR-15 (55 gr) → T3X | UP 2.7 | 0.1 LEFT |
| T3X → AR-15 (69 gr) | DOWN 2.9 | 0.5 RIGHT |
| AR-15 (69 gr) → T3X | UP 2.9 | 0.5 LEFT |

**AR-15 load change (scope stays on):**

| Change | Elevation | Windage |
|---|---|---|
| 55 gr GGG → 69 gr Scenar | DOWN 0.2 | 0.4 RIGHT |
| 69 gr Scenar → 55 gr GGG | UP 0.2 | 0.4 LEFT |

---

## Notes

- AR-15 re-zero (2026-10-16) landed at +1.8 for the 55 gr — 0.5 higher than the old estimate of +1.3. That matches the old zero hitting low, so the numbers are consistent.
- The measured 4.5 MRAD elevation difference between T1X and T3X is under the theoretical 5.8 MRAD (20 MOA) from the T1X's canted rail — mount height and trajectory differences (.22 LR at 50 m vs 6.5 CM at 100 m) account for the rest.
- T1X → T3X was measured at UP 4.5 / 3.7 L, less than the old-frame calculation (UP 5.8 / 4.1 L). Measured values are the ones to trust; confirm with a check shot after the next couple of swaps until they prove repeatable.
- Old dial frame for reference: AR-15 was 0 / 0, T3X +4.5 / 0.5 L, T1X −1.3 / 3.6 R.

## To-do

- [x] Zero T1X and set as 0 / 0 reference, zero stop 0.5 below
- [x] Measure T1X → T3X in new frame: UP 4.5 / 3.7 LEFT
- [x] Re-zero AR-15 with both loads (2026-10-16)
- [ ] Confirm swaps repeat with a check shot over the next few sessions

*Last updated: 2026-10-16 — all three rifles zeroed in the T1X = 0/0 frame.*
