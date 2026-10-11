import { Send, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Markdown, ModelBadge, Sources } from "../components/ui";

const SUGGESTIONS = [
  "How was my driving this week?",
  "Which habit costs me the most points?",
  "Why is my fuel economy what it is?",
  "Is my car healthy right now?",
  "How is the driving score calculated?",
];

export default function Coach({ vehicle }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);

  useEffect(() => setMessages([]), [vehicle.id]);
  useEffect(() => endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }), [messages, busy]);

  const ask = async (question) => {
    const q = question.trim();
    if (!q || busy) return;
    const history = messages.map((m) => ({ role: m.role, text: m.text })).slice(-6);
    setMessages((m) => [...m, { role: "user", text: q }]);
    setInput("");
    setBusy(true);
    try {
      const r = await api.chat(vehicle.id, q, history);
      setMessages((m) => [...m, { role: "assistant", text: r.answer, result: r }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", text: `I could not answer that: ${e.message}` }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel narrow" style={{ padding: 0 }}>
      <div style={{ padding: 18, minHeight: 380 }}>
        <div className="chat">
          {messages.length === 0 && (
            <div>
              <div className="row" style={{ fontWeight: 700, marginBottom: 6 }}>
                <Sparkles size={18} color="var(--green)" />Ask about your driving or your car
              </div>
              <p className="muted" style={{ fontSize: "var(--fs-sm)", marginTop: 0 }}>
                Answers use this vehicle's real trips, scores and sensor readings, plus the driving and
                service guides. Every guide passage used is cited.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => <button key={s} onClick={() => ask(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="bubble user">{m.text}</div>
            ) : (
              <div key={i} className="bubble bot">
                {m.result && <div className="row" style={{ marginBottom: 4 }}><ModelBadge result={m.result} /></div>}
                <Markdown text={m.text} />
                <Sources sources={m.result?.sources} />
              </div>
            )
          )}
          {busy && <div className="bubble bot muted">Looking at your data</div>}
          <div ref={endRef} />
        </div>
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); ask(input); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask your driving coach"
               aria-label="Question for the driving coach" maxLength={1000} />
        <button className="btn primary" type="submit" disabled={busy || !input.trim()} aria-label="Send"><Send size={16} /></button>
      </form>
    </div>
  );
}
