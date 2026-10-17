import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { AppProvider } from './state';
import { BrowserToolbar } from './BrowserToolbar';
import './styles.css';

const loginToolbar = new URLSearchParams(location.search).has('login-toolbar');
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode>{loginToolbar ? <BrowserToolbar /> : <AppProvider><App /></AppProvider>}</React.StrictMode>);
