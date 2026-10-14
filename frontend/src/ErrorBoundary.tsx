import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

/** Shows a crash instead of a blank page, so problems can be reported. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("simcore UI crashed", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div style={{ padding: 24, fontFamily: "monospace", whiteSpace: "pre-wrap" }} data-testid="ui-error">
        <h2>The page hit an error</h2>
        <p>Please copy this text when reporting it:</p>
        <p>{String(error)}</p>
        <p>{error.stack}</p>
        <button onClick={() => location.reload()}>Reload</button>
      </div>
    );
  }
}
