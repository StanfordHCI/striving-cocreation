import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';

class AppErrorBoundary extends React.Component<
  React.PropsWithChildren,
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Uncaught renderer error:', error, info);
  }

  render() {
    return this.state.hasError ? <AppFallback /> : this.props.children;
  }
}

const root = document.getElementById('root');
if (!root) throw new Error('Missing #root mount element');

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  </React.StrictMode>
);

function AppFallback() {
  return (
    <div style={{ padding: 40, textAlign: 'center', color: 'var(--ink)' }}>
      <h2>Something went wrong</h2>
      <p>Please restart the app. Error details are available in the local console.</p>
      <button className="primary-button" onClick={() => window.location.reload()}>
        Reload
      </button>
    </div>
  );
}
