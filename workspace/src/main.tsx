import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import '@fontsource/dm-sans/400.css';
import '@fontsource/dm-sans/500.css';
import '@fontsource/dm-sans/600.css';
import '@fontsource/dm-sans/700.css';
import './styles.css';
import './variator/tour/tour.css';
import App from './App';
import { appPath, BASE, stripBase } from './appUrl';
import { startFirstView } from './firstView';
import { preloadCase } from './variator/case/model/cases';
// The app lives under BASE (/workspace). An address outside it (the workspace server opened at /brief) shows the
// same page under the base, so the router and every link agree.
if (stripBase(BASE, window.location.pathname) === null) {
  const { pathname, search, hash } = window.location;
  window.history.replaceState(window.history.state, '', `${BASE}${pathname}${search}${hash}`);
}
// WP29: the data of the first view is asked for now, while the page's code (src/routes.ts) is still loading
startFirstView(appPath(window.location.pathname), window.location.search, { readCase: preloadCase });
// The code of a page is loaded when it opens: after a new release the files of the old one are gone, and the page
// that asks for one reloads (once a minute at most) to get the new release; else it says so (App: PageBoundary).
window.addEventListener('vite:preloadError', () => {
  try {
    if (Date.now() - Number(sessionStorage.getItem('variator:reloaded') ?? 0) < 60_000) return;
    sessionStorage.setItem('variator:reloaded', String(Date.now()));
  } catch { return; }
  window.location.reload();
});
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter basename={BASE || '/'}><App/></BrowserRouter></React.StrictMode>);
