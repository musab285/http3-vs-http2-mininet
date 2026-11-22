# Report build notes

`report.tex` is the complete LaTeX technical report. It embeds the final plots and Wireshark screenshots from `/home/mmak/Documents/CNProject`.

Build it from this directory with:

```bash
tectonic report.tex
```

Primary source data used by the report:

- `/home/mmak/Documents/CNProject/results/final-instrumented-evaluation/summary.csv`
- `/home/mmak/Documents/CNProject/results/additional-handshake-metrics/summary.csv`
- `/home/mmak/Documents/CNProject/results/additional-workload-metrics/summary.csv`
- `/home/mmak/Documents/CNProject/wiresharkevidence/`

The PDF contains all embedded figures. The `.tex` file uses absolute paths because the experiment project and report workspace are separate folders. For a portable submission, either submit the produced PDF or copy the referenced figure folders alongside `report.tex` and replace the figure paths.
