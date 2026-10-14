import { useStore } from "../store";

/** What can be asked and changed, straight from the catalogs (FR-UI-10/11). */
export function DomainExplorer() {
  const { manifest } = useStore();
  if (!manifest) return <p className="muted">Select a scenario to see its domain.</p>;
  return (
    <div className="explorer" data-testid="domain-explorer">
      <h2>
        {manifest.name} <small className="muted">v{manifest.domain_version} · tier {manifest.tier}</small>
      </h2>
      <p>{manifest.description}</p>
      <h3>Levers</h3>
      <table>
        <tbody>
          {manifest.levers.map((l) => (
            <tr key={l.id}>
              <td>{l.display_name}</td>
              <td>
                <code>{l.id}</code>
              </td>
              <td>{l.type}</td>
              <td>{l.choices?.length ? l.choices.join(", ") : [l.min, l.max].filter((x) => x != null).join(" – ")}</td>
              <td className="muted">{l.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Metrics</h3>
      <table>
        <tbody>
          {manifest.metrics.map((m) => (
            <tr key={m.id}>
              <td>{m.display_name}</td>
              <td>
                <code>{m.id}</code>
              </td>
              <td>{m.unit}</td>
              <td>{m.direction}</td>
              <td className="muted">{m.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Dimensions</h3>
      <p>{manifest.dimensions.map((d) => d.display_name).join(", ")}</p>
      {(manifest.envelope ?? []).length > 0 && (
        <>
          <h3>Calibrated envelope</h3>
          <ul>
            {(manifest.envelope ?? []).map((b) => (
              <li key={b.lever}>
                {b.lever}: {b.min ?? "–"} to {b.max ?? "–"}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
