import { RefreshCw, Sparkles } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Markdown, ModelBadge, Sources } from "./ui";

/** One-click AI analysis of the car's week: driving, fuel and health together. */
export default function WeeklyReport({ car }) {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);

  const run = async (refresh = false) => {
    setLoading(true);
    try {
      setReport(await api.report(car.id, refresh));
    } catch (e) {
      setReport({ answer: `The report could not be created: ${e.message}`, sources: [] });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="panel">
      <div className="row" style={{ marginBottom: report ? 10 : 6 }}>
        <Sparkles size={18} color="var(--green)" />
        <h2 className="panel-title" style={{ margin: 0 }}>AI weekly report</h2>
        {report && <ModelBadge result={report} />}
      </div>
      {!report ? (
        <>
          <p className="muted" style={{ margin: "0 0 12px", fontSize: "var(--fs-sm)" }}>
            Your driving, fuel economy and the car's health over the last 7 days, compared with the week before,
            with one thing to focus on next.
          </p>
          <button className="btn primary" onClick={() => run()} disabled={loading}>
            <Sparkles size={16} />{loading ? "Analysing your week" : "Analyse my week"}
          </button>
        </>
      ) : (
        <div aria-live="polite">
          <Markdown text={report.answer} />
          <Sources sources={report.sources} />
          {report.facts && (
            <details className="facts"><summary>What the AI was told</summary><pre>{report.facts}</pre></details>
          )}
          <button className="link-btn" style={{ marginTop: 10 }} onClick={() => run(true)} disabled={loading}>
            <RefreshCw size={12} /> {loading ? "Updating" : "Update report"}
          </button>
        </div>
      )}
    </div>
  );
}
