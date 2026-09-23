import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import { App } from './app/App';
import { SessionProvider } from './shared/session';
import { SignatureProvider } from './shared/signature';
import { ToastProvider } from './shared/ui';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <ToastProvider>
        <SessionProvider>
          <SignatureProvider>
            <App />
          </SignatureProvider>
        </SessionProvider>
      </ToastProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
