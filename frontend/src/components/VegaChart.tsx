import { useEffect, useRef } from "react";
import embed from "vega-embed";

/** Renders a server-produced Vega-Lite spec (data comes from the governed query). */
export function VegaChart({ spec, testId }: { spec: Record<string, any>; testId?: string }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    let view: { finalize(): void } | undefined;
    embed(el.current, spec as any, { actions: false, renderer: "svg" })
      .then((r) => (view = r.view))
      .catch((e) => {
        if (el.current) el.current.textContent = `chart error: ${e}`;
      });
    return () => view?.finalize();
  }, [spec]);
  return <div className="vega" ref={el} data-testid={testId} />;
}
