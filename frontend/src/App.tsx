import { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route, Navigate, NavLink } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { UploadPage } from './pages/Upload';
import { DashboardPage } from './pages/Dashboard';
import { OverviewPage } from './pages/Overview';
import { TrainingJobsPage } from './pages/TrainingJobs';
import { SIGN_OUT_PATH } from './services/http';

// The Viewer pulls in Three.js (~800 KB). Lazy-load it so the Upload and
// Dashboard pages don't pay for it on first load.
const ViewerPage = lazy(() =>
  import('./pages/Viewer').then((m) => ({ default: m.ViewerPage })),
);

function ViewerFallback() {
  return (
    <div className="flex items-center justify-center h-96">
      <div className="text-center">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-blue-500 mx-auto mb-4" />
        <p className="text-gray-400">Loading 3D viewer...</p>
      </div>
    </div>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false },
  },
});

function NavItem({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        isActive ? 'text-white font-medium' : 'text-gray-300 hover:text-white'
      }
    >
      {children}
    </NavLink>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="min-h-screen bg-gray-900 text-white">
          <nav className="bg-gray-800 border-b border-gray-700 px-6 py-3 flex items-center gap-6">
            <NavLink to="/dashboard" className="text-lg font-bold hover:text-blue-400 transition">
              Point Cloud Detection
            </NavLink>
            <NavItem to="/overview">Overview</NavItem>
            <NavItem to="/dashboard">Detection Jobs</NavItem>
            <NavItem to="/training-jobs">Training Jobs</NavItem>
            <NavItem to="/upload">Upload</NavItem>
            {/*
              A full page load, not a route: nginx and Cognito end the session. Hidden
              under the Vite dev server, which has no session and no /signout.
            */}
            {!import.meta.env.DEV && (
              <a href={SIGN_OUT_PATH} className="ml-auto text-gray-300 hover:text-white">
                Sign out
              </a>
            )}
          </nav>
          <main className="p-6">
            <Suspense fallback={<ViewerFallback />}>
              <Routes>
                <Route path="/" element={<Navigate to="/overview" replace />} />
                <Route path="/upload" element={<UploadPage />} />
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/training-jobs" element={<TrainingJobsPage />} />
                {/*
                  Legacy paths. The real bookmarkable URL when training lived in a
                  Dashboard tab was /dashboard?tab=training — that one is handled in
                  Dashboard itself, since a Route cannot match on a query param.
                  These two are kept as plausible hand-typed paths.
                */}
                <Route path="/training" element={<Navigate to="/training-jobs" replace />} />
                <Route path="/training-sessions" element={<Navigate to="/training-jobs" replace />} />
                <Route path="/overview" element={<OverviewPage />} />
                <Route path="/viewer/:jobId" element={<ViewerPage />} />
              </Routes>
            </Suspense>
          </main>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
