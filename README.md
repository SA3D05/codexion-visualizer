# Codexion Visualizer

## 📌 Description

**Codexion Visualizer** is a Python-based interactive tool designed to visualize the output logs of the Codexion concurrency project.

It transforms raw log lines into a real-time animated simulation:

* Coders are displayed as **people around a circular table**
* A **shared compiler** is shown at the center
* **Dongles** move between the table and coders with animations
* States like **compiling, debugging, refactoring, waiting, and burnout** are visually represented

The goal is to help you:

* Understand thread behavior
* Debug synchronization issues
* See fairness (FIFO / EDF) in action
* Detect starvation or unexpected burnout

---

## 🧠 How It Works

The visualizer parses Codexion logs formatted like:

```
0 1 has taken a dongle
384 4 has taken a left 4 dongle
384 4 has taken a right 1 dongle
1 1 is compiling
201 1 is debugging
401 1 is refactoring
1204 3 burned out
```

### Internal Logic

1. **Parse logs → Events**

   * Extract: timestamp, coder ID, action
   * Detect left/right dongle when available

2. **Build simulation snapshots**

   * Each event updates:

     * coder state
     * dongle state (available / taken / cooldown)

3. **Playback engine**

   * Two modes:

     * **Time mode** → based on real timestamps
     * **Event mode** → fixed delay between events

4. **Rendering (pygame)**

   * Coders drawn as **person icons**
   * Dongles:

     * 🟢 available
     * 🔵 taken
     * 🟠 cooldown
   * Smooth animations between states

---

## 🚀 How to Run

### 1. Install requirements

```bash
python3 -m pip install pygame
```

### 2. Run with a log file

```bash
python3 codexion_visualizer.py --file codex.log --coders 4
```

### 3. Pipe directly from your program

```bash
./codex 4 45 10 10 10 10 10 edf | python3 codexion_visualizer.py --stdin --coders 4
```

### 4. Run codex automatically

```bash
python3 codexion_visualizer.py --run ./codex 4 45 10 10 10 10 10 edf
```

---

## ⚙️ Arguments

| Argument      | Description                  |
| ------------- | ---------------------------- |
| `--file`      | Read logs from file          |
| `--stdin`     | Read logs from stdin         |
| `--run`       | Run codex and capture output |
| `--coders`    | Number of coders             |
| `--burnout`   | time_to_burnout              |
| `--compile`   | time_to_compile              |
| `--debug`     | time_to_debug                |
| `--refactor`  | time_to_refactor             |
| `--required`  | number_of_compiles_required  |
| `--cooldown`  | dongle_cooldown              |
| `--scheduler` | fifo or edf                  |
| `--scale`     | time scaling (default: 0.02) |
| `--step`      | start in step mode           |

---

## 🎮 Controls

### Navigation

* `ENTER` → toggle step / auto mode
* `SPACE` → next event (step) / pause (auto)
* `← / →` → move between events
* `R` → restart
* `G` → jump to last event

### Speed

* `↑ / ↓` → change simulation speed
* `SHIFT + ↑ / ↓` → change event gap (event mode)

### Modes

* `E` → toggle **event mode**
* `H` → toggle help
* `T` → toggle timestamps

### Logs

* `W / S` → scroll logs
* Mouse wheel → scroll logs

### Exit

* `ESC` or `Q`

---

## 🎨 Visual Meaning

### Coder States

| State       | Color |
| ----------- | ----- |
| Waiting     | 🟡    |
| Compiling   | 🔵    |
| Debugging   | 🟢    |
| Refactoring | 🟣    |
| Burned out  | 🔴    |

### Dongle States

| State     | Color |
| --------- | ----- |
| Available | 🟢    |
| Taken     | 🔵    |
| Cooldown  | 🟠    |

---

## 💡 Notes

* Best experience when using **detailed logs**:

  ```
  has taken a left X dongle
  has taken a right X dongle
  ```
* Generic logs (`has taken a dongle`) still work but with limited animation accuracy.
* Event mode is useful to **debug scheduling logic step by step**
* Time mode is useful to **observe real timing behavior**

---

## 🔧 Possible Improvements

* Export simulation to video (GIF / MP4)
* Add heatmap for contention on dongles
* Show EDF priorities visually
* Highlight starvation risks

---

## 📦 Requirements

* Python 3.8+
* pygame

---

## 🧪 Use Case

This tool is especially useful during:

* debugging your Codexion project
* testing fairness (FIFO vs EDF)
* verifying cooldown logic
* preparing for 42 evaluation

---

## ✨ Summary

Instead of reading logs like this:

```
384 4 has taken a left 4 dongle
```

You **see it happen** — coder moves, dongle moves, states change.

That’s the difference.

---
