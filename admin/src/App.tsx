import { useState, useEffect, useRef, useMemo } from 'react';
import {
  ThemeProvider,
  createTheme,
  CssBaseline,
  Container,
  Box,
  Typography,
  Button,
  TextField,
  Card,
  CardContent,
  Grid,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Tabs,
  Tab,
  AppBar,
  Toolbar,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Snackbar,
  Alert,
  Chip,
} from '@mui/material';
import {
  Speed,
  Layers,
  FiberManualRecord,
  Refresh,
  Add,
  Send,
  PlayArrow,
  Pause,
  Delete,
  Search,
  Dns,
  Storage,
  VpnKey,
  ContentCopy,
} from '@mui/icons-material';



// 1. Create a Premium Dark Theme with Vibrant Violet Accent & Glassmorphic variables
const theme = createTheme({
  palette: {
    mode: 'dark',
    primary: {
      main: '#7c4dff', // Vibrant Violet
      light: '#b47cff',
      dark: '#3f1dcb',
    },
    secondary: {
      main: '#00e5ff', // Neon Cyan
    },
    background: {
      default: '#0a0a0c', // Deep near-black
      paper: '#121216',   // Rich dark paper
    },
    text: {
      primary: '#f5f5f7',
      secondary: '#a0a0b0',
    },
  },
  typography: {
    fontFamily: '"Outfit", "Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    h4: {
      fontWeight: 700,
      letterSpacing: '-0.02em',
    },
    h6: {
      fontWeight: 600,
    },
  },
  components: {
    MuiButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          textTransform: 'none',
          fontWeight: 600,
          transition: 'all 0.2s ease-in-out',
          '&:hover': {
            transform: 'translateY(-1px)',
            boxShadow: '0 4px 12px rgba(124, 77, 255, 0.3)',
          },
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 16,
          background: 'rgba(18, 18, 22, 0.7)',
          backdropFilter: 'blur(12px)',
          border: '1px solid rgba(255, 255, 255, 0.05)',
          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          '&:hover': {
            transform: 'translateY(-4px)',
            boxShadow: '0 12px 24px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(124, 77, 255, 0.15)',
          },
        },
      },
    },
  },
});

interface Topic {
  name: string;
  size_bytes: number;
  allocated_blocks: number;
  max_blocks: number;
  max_age_min: number;
}

interface LogEvent {
  offset: number;
  ts: number;
  src: string;
  data: any;
}

export default function App() {
  const [tabValue, setTabValue] = useState(0);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [serverOnline, setServerOnline] = useState(false);

  // Active topic selected for explorer, inject, or streaming
  const [selectedTopic, setSelectedTopic] = useState<string>('');

  // Dialog and Notification controls
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newTopicName, setNewTopicName] = useState('');
  const [notification, setNotification] = useState<{ msg: string; type: 'success' | 'error' | 'info' } | null>(null);

  // Edit Config state
  const [configDialogOpen, setConfigDialogOpen] = useState(false);
  const [configTopicName, setConfigTopicName] = useState('');
  const [configMaxBlocks, setConfigMaxBlocks] = useState<string>('');
  const [configMaxAgeMin, setConfigMaxAgeMin] = useState<string>('');
  const [updatingConfig, setUpdatingConfig] = useState(false);

  // Historical Explorer state
  const [historicalEvents, setHistoricalEvents] = useState<LogEvent[]>([]);
  const [explorerStartIdx, setExplorerStartIdx] = useState<string>('-10'); // negative default relative
  const [explorerLimit, setExplorerLimit] = useState<number>(50);
  const [explorerLoading, setExplorerLoading] = useState(false);

  // Ingestion Composer state
  const [injectPayload, setInjectPayload] = useState<string>('{\n  "event": "device_telemetry",\n  "status": "normal",\n  "metric": 42.5\n}');
  const [injecting, setInjecting] = useState(false);
  const [jsonValid, setJsonValid] = useState(true);

  // SSE Stream Console state
  const [streamActive, setStreamActive] = useState(false);
  const [streamStartIdx, setStreamStartIdx] = useState<string>('-5'); // stream last 5 events on startup
  const [streamEvents, setStreamEvents] = useState<LogEvent[]>([]);
  const [streamThroughput, setStreamThroughput] = useState(0); // events processed in session
  const eventSourceRef = useRef<EventSource | null>(null);
  const consoleBottomRef = useRef<HTMLDivElement | null>(null);

  // API Key Management state
  const [apiKey, setApiKey] = useState<string>('');
  const [showApiKey, setShowApiKey] = useState(false);
  const [editingApiKey, setEditingApiKey] = useState<string>('');
  const [savingApiKey, setSavingApiKey] = useState(false);

  const [readKey, setReadKey] = useState<string>('');
  const [showReadKey, setShowReadKey] = useState(false);
  const [editingReadKey, setEditingReadKey] = useState<string>('');
  const [savingReadKey, setSavingReadKey] = useState(false);

  // Custom Authentication Layer & SSO states
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [loginUsername, setLoginUsername] = useState('admin');
  const [loginPassword, setLoginPassword] = useState('');
  const [loginError, setLoginError] = useState('');

  const getAuthHeader = () => {
    const token = sessionStorage.getItem('sluicegate_auth');
    return token ? { 'Authorization': `Basic ${token}` } : {};
  };

  const authFetch = async (input: RequestInfo, init?: RequestInit) => {
    const headers: Record<string, string> = {};
    const authHeader = getAuthHeader();
    if (authHeader.Authorization) {
      headers['Authorization'] = authHeader.Authorization;
    }
    if (init?.headers) {
      Object.assign(headers, init.headers);
    }
    const res = await fetch(input, {
      ...init,
      headers,
    });
    if (res.status === 401) {
      setIsAuthenticated(false);
      sessionStorage.removeItem('sluicegate_auth');
    }
    return res;
  };

  // URL-based Single Sign-On check and Session caching on mount
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const ssoUser = params.get('sso_user');
    const ssoPass = params.get('sso_pass');

    if (ssoUser && ssoPass) {
      const token = btoa(`${ssoUser}:${ssoPass}`);
      sessionStorage.setItem('sluicegate_auth', token);
      setIsAuthenticated(true);

      // Scrub SSO parameters immediately so they do not persist in address history
      const cleanUrl = window.location.pathname;
      window.history.replaceState({}, document.title, cleanUrl);
    } else {
      const token = sessionStorage.getItem('sluicegate_auth');
      if (token) {
        setIsAuthenticated(true);
      }
    }
  }, []);

  // Fetch API keys and boot metadata poller once authenticated
  useEffect(() => {
    if (isAuthenticated) {
      fetchKeys();
      fetchTopics();
      const interval = setInterval(fetchTopics, 3000);
      return () => clearInterval(interval);
    }
  }, [isAuthenticated]);

  const fetchKeys = async () => {
    try {
      const res1 = await authFetch('/api/system/apikey');
      if (res1.ok) {
        const data1 = await res1.json();
        setApiKey(data1.api_key || '');
        setEditingApiKey(data1.api_key || '');
      }
      const res2 = await authFetch('/api/system/readkey');
      if (res2.ok) {
        const data2 = await res2.json();
        setReadKey(data2.api_key || '');
        setEditingReadKey(data2.api_key || '');
      }
    } catch (err) {
      console.error('Error fetching API keys:', err);
    }
  };

  const fetchTopics = async () => {
    try {
      const res = await authFetch('/api/topics');
      if (res.ok) {
        const data = await res.json();
        setTopics(data.topics || []);
        setServerOnline(true);
        // Default select first topic if none is selected
        if (data.topics && data.topics.length > 0 && !selectedTopic) {
          setSelectedTopic(data.topics[0].name);
        }
      } else {
        setServerOnline(false);
      }
    } catch {
      setServerOnline(false);
    }
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError('');
    const token = btoa(`${loginUsername}:${loginPassword}`);
    try {
      const res = await fetch('/api/topics', {
        headers: { 'Authorization': `Basic ${token}` }
      });
      if (res.ok) {
        sessionStorage.setItem('sluicegate_auth', token);
        setIsAuthenticated(true);
      } else {
        setLoginError('Invalid administrator credentials.');
      }
    } catch {
      setLoginError('Could not connect to Sluicegate server.');
    }
  };

  const handleCreateTopic = async () => {
    if (!newTopicName.trim()) return;
    try {
      const cleanName = newTopicName.trim().replace(/[^a-zA-Z0-9_-]/g, '');
      const res = await authFetch(`/api/inject?topic=${cleanName}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sys: 'topic_bootstrap', ts: Date.now() }),
      });
      if (res.ok) {
        showNotify(`Topic "${cleanName}" created successfully!`, 'success');
        setNewTopicName('');
        setCreateDialogOpen(false);
        fetchTopics();
        setSelectedTopic(cleanName);
      } else {
        showNotify('Failed to create topic', 'error');
      }
    } catch (e) {
      showNotify(`Error creating topic: ${e}`, 'error');
    }
  };

  const handleRegenerateApiKey = () => {
    const randomHex = Array.from({length: 32}, () => Math.floor(Math.random()*16).toString(16)).join('');
    setEditingApiKey("sg_ingest_" + randomHex);
    showNotify('New API Key generated locally! Click "Save Key" to apply changes.', 'info');
  };

  const handleSaveApiKey = async () => {
    if (!editingApiKey.trim() || editingApiKey.trim().length < 8) {
      showNotify('API Key must be at least 8 characters long', 'error');
      return;
    }
    setSavingApiKey(true);
    try {
      const res = await authFetch('/api/system/apikey', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: editingApiKey.trim() })
      });
      if (res.ok) {
        const data = await res.json();
        setApiKey(data.api_key);
        setEditingApiKey(data.api_key);
        showNotify('Ingestion API Key saved successfully!', 'success');
      } else {
        showNotify('Failed to save Ingestion API Key', 'error');
      }
    } catch (e) {
      showNotify(`Failed to save Ingestion API Key: ${e}`, 'error');
    } finally {
      setSavingApiKey(false);
    }
  };

  const handleRegenerateReadKey = () => {
    const randomHex = Array.from({length: 32}, () => Math.floor(Math.random()*16).toString(16)).join('');
    setEditingReadKey("sg_read_" + randomHex);
    showNotify('New Read Key generated locally! Click "Save Key" to apply changes.', 'info');
  };

  const handleSaveReadKey = async () => {
    if (!editingReadKey.trim() || editingReadKey.trim().length < 8) {
      showNotify('Read API Key must be at least 8 characters long', 'error');
      return;
    }
    setSavingReadKey(true);
    try {
      const res = await authFetch('/api/system/readkey', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: editingReadKey.trim() })
      });
      if (res.ok) {
        const data = await res.json();
        setReadKey(data.api_key);
        setEditingReadKey(data.api_key);
        showNotify('Read API Key saved successfully!', 'success');
      } else {
        showNotify('Failed to save Read API Key', 'error');
      }
    } catch (e) {
      showNotify(`Failed to save Read API Key: ${e}`, 'error');
    } finally {
      setSavingReadKey(false);
    }
  };

  const copyToClipboard = (val: string, label: string) => {
    navigator.clipboard.writeText(val);
    showNotify(`${label} copied to clipboard!`, 'info');
  };

  const handleUpdateConfig = async () => {
    if (!configTopicName) return;
    try {
      setUpdatingConfig(true);
      const res = await authFetch(`/api/config?topic=${configTopicName}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_blocks: parseInt(configMaxBlocks) || 104857600,
          max_age_min: parseInt(configMaxAgeMin) || 1440
        }),
      });
      if (res.ok) {
        showNotify(`Configuration for topic "${configTopicName}" updated successfully!`, 'success');
        setConfigDialogOpen(false);
        fetchTopics();
      } else {
        showNotify('Failed to update configuration.', 'error');
      }
    } catch (e) {
      showNotify(`Error updating configuration: ${e}`, 'error');
    } finally {
      setUpdatingConfig(false);
    }
  };

  const handleFetchEvents = async () => {
    if (!selectedTopic) return;
    setExplorerLoading(true);
    try {
      const idx = parseInt(explorerStartIdx) || 0;
      const res = await authFetch(`/api/events?topic=${selectedTopic}&start_idx=${idx}&limit=${explorerLimit}`);
      if (res.ok) {
        const data = await res.json();
        setHistoricalEvents(data.events || []);
        showNotify(`Retrieved ${data.events?.length || 0} events.`, 'success');
      } else {
        showNotify('Failed to fetch historical events.', 'error');
      }
    } catch (e) {
      showNotify(`Error: ${e}`, 'error');
    } finally {
      setExplorerLoading(false);
    }
  };

  const handleInjectEvent = async () => {
    if (!selectedTopic) return;
    try {
      const parsed = JSON.parse(injectPayload);
      setInjecting(true);
      const res = await authFetch(`/api/inject?topic=${selectedTopic}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(parsed),
      });
      if (res.ok) {
        const data = await res.json();
        showNotify(`Event successfully injected at offset ${data.offset}!`, 'success');
      } else {
        showNotify('Failed to inject event.', 'error');
      }
    } catch (e) {
      showNotify(`JSON Parse Error: ${e}`, 'error');
    } finally {
      setInjecting(false);
    }
  };

  const handlePayloadChange = (val: string) => {
    setInjectPayload(val);
    try {
      JSON.parse(val);
      setJsonValid(true);
    } catch {
      setJsonValid(false);
    }
  };

  // SSE Stream subscription controls
  useEffect(() => {
    if (streamActive && selectedTopic) {
      startStream();
    } else {
      stopStream();
    }
    return () => stopStream();
  }, [streamActive, selectedTopic]);

  const startStream = () => {
    stopStream();
    setStreamEvents([]);
    setStreamThroughput(0);

    const readKeyParam = readKey ? `&read_key=${encodeURIComponent(readKey)}` : '';
    const startIdxParam = streamStartIdx.trim() ? `&start_idx=${parseInt(streamStartIdx) || 0}` : '';
    const url = `/stream?topic=${selectedTopic}${startIdxParam}${readKeyParam}`;
    
    console.log(`[SSE] Connecting to: ${url}`);
    const source = new EventSource(url);
    eventSourceRef.current = source;

    source.onmessage = (event) => {
      try {
        const packet = JSON.parse(event.data);
        setStreamEvents((prev) => {
          // Keep only the last 150 events in UI buffer to prevent memory leakage
          const updated = [...prev, packet];
          return updated.slice(-150);
        });
        setStreamThroughput((prev) => prev + 1);
      } catch (e) {
        console.error('Failed to parse SSE payload:', e);
      }
    };

    source.onerror = (e) => {
      console.error('[SSE] Disconnected or encountered error:', e);
      showNotify('Stream link interrupted. Reconnecting...', 'info');
    };
  };

  const stopStream = () => {
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
      console.log('[SSE] Stream closed.');
    }
  };

  // Scroll stream console to bottom on new event arrivals
  useEffect(() => {
    if (consoleBottomRef.current) {
      consoleBottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [streamEvents]);

  const showNotify = (msg: string, type: 'success' | 'error' | 'info') => {
    setNotification({ msg, type });
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  // Memoized stats calculation for dashboard
  const systemStats = useMemo(() => {
    const totalSize = topics.reduce((acc, t) => acc + t.size_bytes, 0);
    const totalBlocks = topics.reduce((acc, t) => acc + t.allocated_blocks, 0);
    return { totalSize, totalBlocks };
  }, [topics]);

  if (!isAuthenticated) {
    return (
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <Box sx={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'radial-gradient(circle at center, #1e1035 0%, #0a0a0c 100%)',
          p: 2
        }}>
          <Card sx={{
            maxWidth: 400,
            width: '100%',
            p: 4,
            background: 'rgba(18, 18, 22, 0.65)',
            backdropFilter: 'blur(20px)',
            border: '1px solid rgba(255, 255, 255, 0.08)',
            boxShadow: '0 20px 40px rgba(0,0,0,0.6), 0 0 40px rgba(124, 77, 255, 0.15)',
            transition: 'none !important',
            '&:hover': {
              transform: 'none !important',
              boxShadow: '0 20px 40px rgba(0,0,0,0.6), 0 0 40px rgba(124, 77, 255, 0.25) !important'
            }
          }}>
            <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mb: 4 }}>
              <Box sx={{
                p: 2,
                borderRadius: 4,
                background: 'linear-gradient(135deg, #7c4dff, #00e5ff)',
                display: 'flex',
                boxShadow: '0 8px 24px rgba(124, 77, 255, 0.3)',
                mb: 2
              }}>
                <Storage sx={{ color: '#fff', fontSize: 32 }} />
              </Box>
              <Typography variant="h4" sx={{
                fontWeight: 800,
                letterSpacing: '-0.04em',
                background: 'linear-gradient(45deg, #f5f5f7, #a0a0b0)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
                mb: 1
              }}>
                SLUICEGATE
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', fontSize: '0.75rem' }}>
                Edge Telemetry Admin Login
              </Typography>
            </Box>

            <form onSubmit={handleLogin}>
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                <TextField
                  fullWidth
                  label="Username"
                  value={loginUsername}
                  onChange={(e) => setLoginUsername(e.target.value)}
                  variant="outlined"
                  required
                />
                <TextField
                  fullWidth
                  label="Password"
                  type="password"
                  value={loginPassword}
                  onChange={(e) => setLoginPassword(e.target.value)}
                  variant="outlined"
                  required
                  autoFocus
                />
                {loginError && (
                  <Typography variant="body2" color="error" sx={{ fontWeight: 600, textAlign: 'center' }}>
                    {loginError}
                  </Typography>
                )}
                <Button
                  type="submit"
                  variant="contained"
                  color="primary"
                  size="large"
                  sx={{
                    py: 1.5,
                    fontSize: '1rem',
                    background: 'linear-gradient(135deg, #7c4dff, #00e5ff)',
                    '&:hover': {
                      background: 'linear-gradient(135deg, #6c3dec, #00d5ee)',
                    }
                  }}
                >
                  Authenticate
                </Button>
              </Box>
            </form>
          </Card>
        </Box>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', bgcolor: 'background.default' }}>
        
        {/* Header App Bar */}
        <AppBar position="static" elevation={0} sx={{ background: 'rgba(18, 18, 22, 0.8)', backdropFilter: 'blur(12px)', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
          <Container maxWidth="xl">
            <Toolbar disableGutters sx={{ justifyContent: 'space-between' }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
                <Box sx={{ p: 1, borderRadius: 2, background: 'linear-gradient(135deg, #7c4dff, #00e5ff)', display: 'flex' }}>
                  <Storage sx={{ color: '#fff' }} />
                </Box>
                <Typography variant="h5" component="div" sx={{ fontWeight: 800, letterSpacing: '-0.03em', background: 'linear-gradient(45deg, #f5f5f7, #a0a0b0)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent' }}>
                  SLUICEGATE
                </Typography>
                <Chip label="Edge Admin" size="small" color="primary" sx={{ fontWeight: 700, ml: 1, height: 20 }} />
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Typography variant="body2" color="text.secondary">Topic Selection:</Typography>
                  <TextField
                    select
                    size="small"
                    value={selectedTopic}
                    onChange={(e) => setSelectedTopic(e.target.value)}
                    slotProps={{ select: { native: true } }}
                    sx={{
                      '& .MuiInputBase-input': { py: 0.6, fontSize: '0.875rem', fontWeight: 600 },
                      bgcolor: 'rgba(255,255,255,0.03)',
                      borderRadius: 2,
                    }}
                  >
                    {topics.map((t) => (
                      <option key={t.name} value={t.name} style={{ backgroundColor: '#121216' }}>{t.name}</option>
                    ))}
                  </TextField>
                </Box>

                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
                  <FiberManualRecord sx={{ color: serverOnline ? '#4caf50' : '#f44336', fontSize: 14 }} />
                  <Typography variant="body2" sx={{ fontWeight: 700, color: serverOnline ? '#4caf50' : '#f44336' }}>
                    {serverOnline ? 'SERVER ONLINE' : 'DISCONNECTED'}
                  </Typography>
                </Box>
              </Box>
            </Toolbar>
          </Container>
        </AppBar>

        {/* Dynamic Page Content */}
        <Container maxWidth="xl" sx={{ mt: 4, mb: 4, flexGrow: 1 }}>
          <Tabs
            value={tabValue}
            onChange={(_, val) => setTabValue(val)}
            textColor="primary"
            indicatorColor="primary"
            sx={{
              mb: 4,
              borderBottom: '1px solid rgba(255, 255, 255, 0.05)',
              '& .MuiTab-root': { fontWeight: 700, fontSize: '0.95rem', minWidth: 120 }
            }}
          >
            <Tab label="Dashboard" />
            <Tab label="Topics Manager" />
            <Tab label="Event Explorer" />
            <Tab label="Live Console" />
            <Tab label="Ingestion Key" />
          </Tabs>


          {/* TAB 0: DASHBOARD */}
          {tabValue === 0 && (
            <Box>
              <Grid container spacing={3} sx={{ mb: 4 }}>
                <Grid size={{ xs: 12, md: 4 }}>
                  <Card>
                    <CardContent sx={{ display: 'flex', alignItems: 'center', gap: 3, p: 3 }}>
                      <Box sx={{ p: 2, borderRadius: 4, bgcolor: 'rgba(124, 77, 255, 0.1)', color: 'primary.main' }}>
                        <Layers fontSize="large" />
                      </Box>
                      <Box>
                        <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 700, textTransform: 'uppercase' }}>Active Topics</Typography>
                        <Typography variant="h4">{topics.length}</Typography>
                      </Box>
                    </CardContent>
                  </Card>
                </Grid>
                <Grid size={{ xs: 12, md: 4 }}>
                  <Card>
                    <CardContent sx={{ display: 'flex', alignItems: 'center', gap: 3, p: 3 }}>
                      <Box sx={{ p: 2, borderRadius: 4, bgcolor: 'rgba(0, 229, 255, 0.1)', color: 'secondary.main' }}>
                        <Storage fontSize="large" />
                      </Box>
                      <Box>
                        <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 700, textTransform: 'uppercase' }}>Physical Disk Allocated</Typography>
                        <Typography variant="h4">{formatBytes(systemStats.totalSize)}</Typography>
                      </Box>
                    </CardContent>
                  </Card>
                </Grid>
                <Grid size={{ xs: 12, md: 4 }}>
                  <Card>
                    <CardContent sx={{ display: 'flex', alignItems: 'center', gap: 3, p: 3 }}>
                      <Box sx={{ p: 2, borderRadius: 4, bgcolor: 'rgba(76, 175, 80, 0.1)', color: '#4caf50' }}>
                        <Dns fontSize="large" />
                      </Box>
                      <Box>
                        <Typography variant="body2" color="text.secondary" sx={{ fontWeight: 700, textTransform: 'uppercase' }}>Total Inode Blocks</Typography>
                        <Typography variant="h4">{systemStats.totalBlocks}</Typography>
                      </Box>
                    </CardContent>
                  </Card>
                </Grid>
              </Grid>

              {/* Main Quick Action Panels */}
              <Grid container spacing={4}>
                {/* Visual Quick Event Injector */}
                <Grid size={{ xs: 12, md: 6 }}>
                  <Card sx={{ height: '100%' }}>
                    <CardContent sx={{ p: 3, display: 'flex', flexDirection: 'column', height: '100%' }}>
                      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                        <Typography variant="h6" sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                          <Send color="primary" /> Quick Event Ingest
                        </Typography>
                        {selectedTopic && <Chip label={`Target: ${selectedTopic}`} size="small" variant="outlined" color="primary" />}
                      </Box>
                      
                      <TextField
                        multiline
                        rows={6}
                        fullWidth
                        value={injectPayload}
                        onChange={(e) => handlePayloadChange(e.target.value)}
                        variant="outlined"
                        error={!jsonValid}
                        helperText={!jsonValid ? 'Syntax Error: Invalid JSON Format' : 'Composer matches Sluicegate raw event payload parameters.'}
                        sx={{
                          mb: 3,
                          flexGrow: 1,
                          '& .MuiInputBase-input': { fontFamily: 'monospace', fontSize: '0.875rem' }
                        }}
                      />

                      <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 2 }}>
                        <Button
                          variant="contained"
                          color="primary"
                          disabled={injecting || !jsonValid || !selectedTopic}
                          onClick={handleInjectEvent}
                          startIcon={injecting ? <CircularProgress size={20} color="inherit" /> : <Send />}
                          sx={{ px: 4 }}
                        >
                          {injecting ? 'Ingesting...' : 'Append to Stream'}
                        </Button>
                      </Box>
                    </CardContent>
                  </Card>
                </Grid>

                {/* Quick Telemetry Summary */}
                <Grid size={{ xs: 12, md: 6 }}>
                  <Card sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
                    <CardContent sx={{ p: 3, flexGrow: 1 }}>
                      <Typography variant="h6" sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 3 }}>
                        <Speed color="secondary" /> Engine Status & Config
                      </Typography>
                      
                      {selectedTopic ? (
                        (() => {
                          const topObj = topics.find(t => t.name === selectedTopic);
                          return (
                            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                              <Box sx={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid rgba(255,255,255,0.05)', pb: 1.5 }}>
                                <Typography color="text.secondary">Current Active Stream:</Typography>
                                <Typography sx={{ fontWeight: 700, color: 'secondary.main' }}>{selectedTopic}.json</Typography>
                              </Box>
                              <Box sx={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid rgba(255,255,255,0.05)', pb: 1.5 }}>
                                <Typography color="text.secondary">Physical File Size:</Typography>
                                <Typography sx={{ fontWeight: 700 }}>{topObj ? formatBytes(topObj.size_bytes) : 'N/A'}</Typography>
                              </Box>
                              <Box sx={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid rgba(255,255,255,0.05)', pb: 1.5 }}>
                                <Typography color="text.secondary">Max Allocation Limit (Blocks):</Typography>
                                <Typography sx={{ fontWeight: 700 }}>{topObj ? `${topObj.max_blocks.toLocaleString()} sectors` : 'N/A'}</Typography>
                              </Box>
                              <Box sx={{ display: 'flex', justifyContent: 'space-between', borderBottom: '1px solid rgba(255,255,255,0.05)', pb: 1.5 }}>
                                <Typography color="text.secondary">Retention cutoff time:</Typography>
                                <Typography sx={{ fontWeight: 700 }}>{topObj ? `${topObj.max_age_min} minutes` : 'N/A'}</Typography>
                              </Box>
                            </Box>
                          );
                        })()
                      ) : (
                        <Box sx={{ py: 6, textAlignment: 'center' }}>
                          <Typography color="text.secondary">No topics selected or discovered.</Typography>
                        </Box>
                      )}
                    </CardContent>
                  </Card>
                </Grid>
              </Grid>
            </Box>
          )}

          {/* TAB 1: TOPIC MANAGER */}
          {tabValue === 1 && (
            <Box>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
                <Typography variant="h5" sx={{ fontWeight: 700 }}>Active Sequential Topic Streams</Typography>
                <Box sx={{ display: 'flex', gap: 2 }}>
                  <Button variant="outlined" color="primary" onClick={fetchTopics} startIcon={<Refresh />}>
                    Refresh Stats
                  </Button>
                  <Button variant="contained" color="primary" onClick={() => setCreateDialogOpen(true)} startIcon={<Add />}>
                    Create New Topic
                  </Button>
                </Box>
              </Box>

              <Grid container spacing={3}>
                {topics.map((t) => (
                  <Grid size={{ xs: 12, sm: 6, md: 4 }} key={t.name}>
                    <Card>
                      <CardContent sx={{ p: 3 }}>
                        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                          <Typography variant="h6" sx={{ fontWeight: 700, color: 'primary.light' }}>{t.name}</Typography>
                          <Chip label={`${t.allocated_blocks} blocks`} size="small" color="secondary" variant="outlined" />
                        </Box>
                        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, mb: 2 }}>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                            <Typography variant="body2" color="text.secondary">Physical size:</Typography>
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>{formatBytes(t.size_bytes)}</Typography>
                          </Box>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                            <Typography variant="body2" color="text.secondary">Sectors allocated:</Typography>
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>{t.allocated_blocks}</Typography>
                          </Box>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                            <Typography variant="body2" color="text.secondary">Max blocks cap:</Typography>
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>{t.max_blocks.toLocaleString()}</Typography>
                          </Box>
                          <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
                            <Typography variant="body2" color="text.secondary">Age threshold:</Typography>
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>{t.max_age_min} minutes</Typography>
                          </Box>
                        </Box>
                        <Box sx={{ display: 'flex', gap: 1.5 }}>
                          <Button size="small" variant="contained" fullWidth onClick={() => { setSelectedTopic(t.name); setTabValue(2); }}>
                            Explore Events
                          </Button>
                          <Button size="small" variant="outlined" fullWidth onClick={() => { setSelectedTopic(t.name); setTabValue(3); }}>
                            Live Console
                          </Button>
                        </Box>
                        <Box sx={{ display: 'flex', gap: 1.5, mt: 1.5 }}>
                          <Button size="small" variant="outlined" color="secondary" fullWidth onClick={() => {
                            setConfigTopicName(t.name);
                            setConfigMaxBlocks(t.max_blocks.toString());
                            setConfigMaxAgeMin(t.max_age_min.toString());
                            setConfigDialogOpen(true);
                          }}>
                            Edit Config
                          </Button>
                        </Box>
                      </CardContent>
                    </Card>
                  </Grid>
                ))}
              </Grid>
            </Box>
          )}

          {/* TAB 2: EVENT EXPLORER */}
          {tabValue === 2 && (
            <Box>
              <Card sx={{ mb: 4 }}>
                <CardContent sx={{ p: 3 }}>
                  <Typography variant="h6" sx={{ mb: 3, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Search /> Log Query Selector & Seek Parameters
                  </Typography>
                  <Grid container spacing={3} sx={{ alignItems: 'center' }}>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <TextField
                        select
                        fullWidth
                        label="Selected Topic"
                        value={selectedTopic}
                        onChange={(e) => setSelectedTopic(e.target.value)}
                        slotProps={{ select: { native: true } }}
                      >
                        {topics.map((t) => (
                          <option key={t.name} value={t.name} style={{ backgroundColor: '#121216' }}>{t.name}</option>
                        ))}
                      </TextField>
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <TextField
                        fullWidth
                        label="Start Pointer Index"
                        value={explorerStartIdx}
                        onChange={(e) => setExplorerStartIdx(e.target.value)}
                        helperText="Use negative for relative backward seek"
                      />
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <TextField
                        fullWidth
                        type="number"
                        label="Limit Count"
                        value={explorerLimit}
                        onChange={(e) => setExplorerLimit(parseInt(e.target.value) || 50)}
                      />
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <Button
                        variant="contained"
                        color="primary"
                        fullWidth
                        size="large"
                        disabled={explorerLoading || !selectedTopic}
                        onClick={handleFetchEvents}
                        startIcon={explorerLoading ? <CircularProgress size={20} color="inherit" /> : <Search />}
                      >
                        Fetch Records
                      </Button>
                    </Grid>
                  </Grid>
                </CardContent>
              </Card>

              {/* Event Table View */}
              <TableContainer component={Paper} sx={{ borderRadius: 4, border: '1px solid rgba(255,255,255,0.05)', overflow: 'hidden' }}>
                <Table>
                  <TableHead sx={{ bgcolor: 'rgba(124, 77, 255, 0.05)' }}>
                    <TableRow>
                      <TableCell sx={{ fontWeight: 700 }}>Record Offset</TableCell>
                      <TableCell sx={{ fontWeight: 700 }}>Timestamp</TableCell>
                      <TableCell sx={{ fontWeight: 700 }}>Ingest Source</TableCell>
                      <TableCell sx={{ fontWeight: 700 }}>Payload Data</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {historicalEvents.length > 0 ? (
                      historicalEvents.map((evt) => (
                        <TableRow key={evt.offset} sx={{ '&:last-child td, &:last-child th': { border: 0 } }}>
                          <TableCell sx={{ fontFamily: 'monospace', fontWeight: 600, color: 'secondary.main' }}>
                            {evt.offset.toLocaleString()}
                          </TableCell>
                          <TableCell sx={{ color: 'text.secondary' }}>
                            {new Date(evt.ts * 1000).toLocaleString()}
                          </TableCell>
                          <TableCell>
                            <Chip label={evt.src} size="small" color="primary" variant="outlined" sx={{ height: 20 }} />
                          </TableCell>
                          <TableCell sx={{ fontFamily: 'monospace', fontSize: '0.825rem' }}>
                            {JSON.stringify(evt.data)}
                          </TableCell>
                        </TableRow>
                      ))
                    ) : (
                      <TableRow>
                        <TableCell colSpan={4} align="center" sx={{ py: 6, color: 'text.secondary' }}>
                          No records loaded. Adjust query index and click "Fetch Records".
                        </TableCell>
                      </TableRow>
                    )}
                  </TableBody>
                </Table>
              </TableContainer>
            </Box>
          )}

          {/* TAB 3: LIVE SSE CONSOLE */}
          {tabValue === 3 && (
            <Box>
              <Card sx={{ mb: 4 }}>
                <CardContent sx={{ p: 3 }}>
                  <Typography variant="h6" sx={{ mb: 3, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Speed /> Real-Time Reactive SSE Feed Setup
                  </Typography>
                  <Grid container spacing={3} sx={{ alignItems: 'center' }}>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <TextField
                        select
                        fullWidth
                        label="Target Topic"
                        value={selectedTopic}
                        onChange={(e) => setSelectedTopic(e.target.value)}
                        slotProps={{ select: { native: true } }}
                      >
                        {topics.map((t) => (
                          <option key={t.name} value={t.name} style={{ backgroundColor: '#121216' }}>{t.name}</option>
                        ))}
                      </TextField>
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <TextField
                        fullWidth
                        label="Historical Catch-up Offset"
                        value={streamStartIdx}
                        onChange={(e) => setStreamStartIdx(e.target.value)}
                        helperText="Negative for relative seeks from EOF"
                      />
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }}>
                      <Button
                        variant="contained"
                        color={streamActive ? 'error' : 'primary'}
                        fullWidth
                        size="large"
                        disabled={!selectedTopic}
                        onClick={() => setStreamActive(!streamActive)}
                        startIcon={streamActive ? <Pause /> : <PlayArrow />}
                      >
                        {streamActive ? 'Stop Stream' : 'Subscribe Now'}
                      </Button>
                    </Grid>
                    <Grid size={{ xs: 12, sm: 3 }} sx={{ display: 'flex', gap: 2 }}>
                      <Button
                        variant="outlined"
                        color="primary"
                        fullWidth
                        size="large"
                        onClick={() => setStreamEvents([])}
                        startIcon={<Delete />}
                      >
                        Clear Terminal
                      </Button>
                    </Grid>
                  </Grid>
                </CardContent>
              </Card>

              {/* Console Output Screen */}
              <Box sx={{ display: 'flex', flexDirection: 'column', borderRadius: 4, border: '1px solid rgba(255,255,255,0.05)', overflow: 'hidden', height: 450, bgcolor: '#000' }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', px: 3, py: 1.5, borderBottom: '1px solid rgba(255,255,255,0.05)', bgcolor: 'rgba(255,255,255,0.02)' }}>
                  <Typography variant="body2" sx={{ fontFamily: 'monospace', fontWeight: 600, color: 'text.secondary' }}>
                    TERMINAL SSE RECEIVER: {selectedTopic || 'none'}
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 2 }}>
                    <Chip label={`Live Frame Count: ${streamThroughput}`} size="small" color="secondary" sx={{ height: 20 }} />
                    <Chip label={streamActive ? 'SUBSCRIBED' : 'IDLE'} size="small" color={streamActive ? 'success' : 'default'} sx={{ height: 20 }} />
                  </Box>
                </Box>
                
                {/* Running Log Feed */}
                <Box sx={{ flexGrow: 1, p: 3, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 1 }}>
                  {streamEvents.length > 0 ? (
                    streamEvents.map((evt, idx) => (
                      <Box key={idx} sx={{ fontFamily: 'monospace', fontSize: '0.85rem', display: 'flex', gap: 2 }}>
                        <Typography component="span" sx={{ color: 'primary.light', fontWeight: 600 }}>
                          [{new Date(evt.ts * 1000).toLocaleTimeString()}]
                        </Typography>
                        <Typography component="span" sx={{ color: 'secondary.main', fontWeight: 600 }}>
                          ({evt.src})
                        </Typography>
                        <Typography component="span" sx={{ color: '#fff' }}>
                          {JSON.stringify(evt.data)}
                        </Typography>
                      </Box>
                    ))
                  ) : (
                    <Box sx={{ display: 'flex', flexGrow: 1, alignItems: 'center', justifyContent: 'center', color: 'text.secondary', fontFamily: 'monospace' }}>
                      {streamActive ? 'Waiting for reactive appends...' : 'Subscribe to start streaming events.'}
                    </Box>
                  )}
                  <div ref={consoleBottomRef} />
                </Box>
              </Box>
            </Box>
          )}

          {/* TAB 4: API KEY SECURITY */}
          {tabValue === 4 && (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {/* Card 1: Ingestion Key Manager */}
              <Card sx={{ maxWidth: 800, mx: 'auto', p: 1, width: '100%' }}>
                <CardContent sx={{ p: 4 }}>
                  <Typography variant="h5" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1.5, mb: 2, color: 'primary.light' }}>
                    <VpnKey fontSize="large" /> Ingestion Key Manager
                  </Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 4, lineHeight: 1.6 }}>
                    Configure and manage the Pre-Shared Ingestion API Key (prefixed with <code>sg_ingest_</code>). 
                    All edge devices, IoT telemetry gatherers, or FastCGI clients MUST supply this key in the 
                    <code>X-Sluicegate-API-Key</code> HTTP header or the <code>api_key</code> query parameter 
                    to write telemetry data.
                  </Typography>

                  <Box sx={{ mb: 4 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1, textTransform: 'uppercase', color: 'text.secondary', fontSize: '0.75rem', letterSpacing: '0.05em' }}>
                      Current Ingestion API Key
                    </Typography>
                    <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
                      <TextField
                        fullWidth
                        type={showApiKey ? 'text' : 'password'}
                        value={apiKey}
                        slotProps={{
                          input: {
                            readOnly: true,
                            sx: { fontFamily: 'monospace', letterSpacing: showApiKey ? 'normal' : '0.1em', fontWeight: 600 }
                          }
                        }}
                      />
                      <Button
                        variant="outlined"
                        onClick={() => setShowApiKey(!showApiKey)}
                        sx={{ minWidth: 100, height: 56 }}
                      >
                        {showApiKey ? 'Hide' : 'Show'}
                      </Button>
                      <Button
                        variant="outlined"
                        color="secondary"
                        onClick={() => copyToClipboard(apiKey, 'Ingestion API Key')}
                        startIcon={<ContentCopy />}
                        sx={{ minWidth: 100, height: 56 }}
                      >
                        Copy
                      </Button>
                    </Box>
                  </Box>

                  <Box sx={{ borderTop: '1px solid rgba(255,255,255,0.05)', pt: 4, mb: 2 }}>
                    <Typography variant="h6" sx={{ fontWeight: 600, mb: 2, fontSize: '1.1rem' }}>
                      Update Ingestion Key
                    </Typography>
                    <Grid container spacing={3}>
                      <Grid size={{ xs: 12, sm: 8 }}>
                        <TextField
                          fullWidth
                          label="New Ingestion Key"
                          value={editingApiKey}
                          onChange={(e) => setEditingApiKey(e.target.value)}
                          placeholder="Enter key (prefixed with sg_ingest_)"
                          helperText="Key will be automatically prefixed with 'sg_ingest_' if not supplied."
                          slotProps={{ input: { sx: { fontFamily: 'monospace' } } }}
                        />
                      </Grid>
                      <Grid size={{ xs: 12, sm: 4 }} sx={{ display: 'flex', gap: 1.5, height: 56 }}>
                        <Button
                          variant="contained"
                          color="primary"
                          fullWidth
                          onClick={handleSaveApiKey}
                          disabled={savingApiKey || editingApiKey.trim() === apiKey || editingApiKey.trim().length < 8}
                        >
                          {savingApiKey ? 'Saving...' : 'Save Key'}
                        </Button>
                      </Grid>
                    </Grid>
                  </Box>

                  <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 2 }}>
                    <Button
                      variant="outlined"
                      color="error"
                      onClick={handleRegenerateApiKey}
                      sx={{
                        borderStyle: 'dashed',
                        '&:hover': { borderStyle: 'solid', bgcolor: 'rgba(244, 67, 54, 0.05)' }
                      }}
                    >
                      Regenerate Locally
                    </Button>
                  </Box>
                </CardContent>
              </Card>

              {/* Card 2: Read Access Key Manager */}
              <Card sx={{ maxWidth: 800, mx: 'auto', p: 1, width: '100%' }}>
                <CardContent sx={{ p: 4 }}>
                  <Typography variant="h5" sx={{ fontWeight: 700, display: 'flex', alignItems: 'center', gap: 1.5, mb: 2, color: 'secondary.main' }}>
                    <VpnKey fontSize="large" /> Read Access Key Manager
                  </Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 4, lineHeight: 1.6 }}>
                    Configure and manage the Pre-Shared Read Access Key (prefixed with <code>sg_read_</code>). 
                    All data visualization dashboards, programmatic subscribers, or SSE clients MUST supply this key in the 
                    <code>X-Sluicegate-Read-Key</code> HTTP header or the <code>read_key</code> query parameter 
                    to pull historical events or subscribe to real-time streams.
                  </Typography>

                  <Box sx={{ mb: 4 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1, textTransform: 'uppercase', color: 'text.secondary', fontSize: '0.75rem', letterSpacing: '0.05em' }}>
                      Current Read Access Key
                    </Typography>
                    <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
                      <TextField
                        fullWidth
                        type={showReadKey ? 'text' : 'password'}
                        value={readKey}
                        slotProps={{
                          input: {
                            readOnly: true,
                            sx: { fontFamily: 'monospace', letterSpacing: showReadKey ? 'normal' : '0.1em', fontWeight: 600 }
                          }
                        }}
                      />
                      <Button
                        variant="outlined"
                        onClick={() => setShowReadKey(!showReadKey)}
                        sx={{ minWidth: 100, height: 56 }}
                      >
                        {showReadKey ? 'Hide' : 'Show'}
                      </Button>
                      <Button
                        variant="outlined"
                        color="secondary"
                        onClick={() => copyToClipboard(readKey, 'Read Access Key')}
                        startIcon={<ContentCopy />}
                        sx={{ minWidth: 100, height: 56 }}
                      >
                        Copy
                      </Button>
                    </Box>
                  </Box>

                  <Box sx={{ borderTop: '1px solid rgba(255,255,255,0.05)', pt: 4, mb: 2 }}>
                    <Typography variant="h6" sx={{ fontWeight: 600, mb: 2, fontSize: '1.1rem' }}>
                      Update Read Access Key
                    </Typography>
                    <Grid container spacing={3}>
                      <Grid size={{ xs: 12, sm: 8 }}>
                        <TextField
                          fullWidth
                          label="New Read Key"
                          value={editingReadKey}
                          onChange={(e) => setEditingReadKey(e.target.value)}
                          placeholder="Enter key (prefixed with sg_read_)"
                          helperText="Key will be automatically prefixed with 'sg_read_' if not supplied."
                          slotProps={{ input: { sx: { fontFamily: 'monospace' } } }}
                        />
                      </Grid>
                      <Grid size={{ xs: 12, sm: 4 }} sx={{ display: 'flex', gap: 1.5, height: 56 }}>
                        <Button
                          variant="contained"
                          color="secondary"
                          fullWidth
                          onClick={handleSaveReadKey}
                          disabled={savingReadKey || editingReadKey.trim() === readKey || editingReadKey.trim().length < 8}
                        >
                          {savingReadKey ? 'Saving...' : 'Save Key'}
                        </Button>
                      </Grid>
                    </Grid>
                  </Box>

                  <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 2 }}>
                    <Button
                      variant="outlined"
                      color="error"
                      onClick={handleRegenerateReadKey}
                      sx={{
                        borderStyle: 'dashed',
                        '&:hover': { borderStyle: 'solid', bgcolor: 'rgba(244, 67, 54, 0.05)' }
                      }}
                    >
                      Regenerate Locally
                    </Button>
                  </Box>
                </CardContent>
              </Card>
            </Box>
          )}

        </Container>



        {/* Create Topic Dialog */}
        <Dialog open={createDialogOpen} onClose={() => setCreateDialogOpen(false)}>
          <DialogTitle sx={{ fontWeight: 700 }}>Bootstrap New Topic Stream</DialogTitle>
          <DialogContent sx={{ minWidth: 350 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
              Initialize a clean, high-performance sequential data stream topic. 
              The server will dynamically map this topic to disk and create its metadata descriptors.
            </Typography>
            <TextField
              autoFocus
              fullWidth
              label="Topic Name"
              value={newTopicName}
              onChange={(e) => setNewTopicName(e.target.value)}
              placeholder="e.g. topic_telemetry_iot"
              variant="outlined"
            />
          </DialogContent>
          <DialogActions sx={{ px: 3, pb: 3 }}>
            <Button onClick={() => setCreateDialogOpen(false)} color="inherit">Cancel</Button>
            <Button onClick={handleCreateTopic} variant="contained" color="primary">Bootstrap</Button>
          </DialogActions>
        </Dialog>

        {/* Edit Config Dialog */}
        <Dialog open={configDialogOpen} onClose={() => setConfigDialogOpen(false)}>
          <DialogTitle sx={{ fontWeight: 700 }}>Edit Topic Configuration</DialogTitle>
          <DialogContent sx={{ minWidth: 350 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
              Update inode extended attribute (xattr) config parameters for topic <strong>{configTopicName}</strong>. 
              Changes are reactively caught and enforced instantly.
            </Typography>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 1 }}>
              <TextField
                fullWidth
                label="Max Sectors (Blocks Limit)"
                value={configMaxBlocks}
                onChange={(e) => setConfigMaxBlocks(e.target.value)}
                placeholder="e.g. 104857600"
                variant="outlined"
              />
              <TextField
                fullWidth
                label="Max Retention Age (Minutes)"
                value={configMaxAgeMin}
                onChange={(e) => setConfigMaxAgeMin(e.target.value)}
                placeholder="e.g. 1440"
                variant="outlined"
              />
            </Box>
          </DialogContent>
          <DialogActions sx={{ px: 3, pb: 3 }}>
            <Button onClick={() => setConfigDialogOpen(false)} color="inherit">Cancel</Button>
            <Button onClick={handleUpdateConfig} variant="contained" color="secondary" disabled={updatingConfig}>
              {updatingConfig ? 'Saving...' : 'Apply Config'}
            </Button>
          </DialogActions>
        </Dialog>

        {/* Global Notifications snackbar */}
        <Snackbar
          open={!!notification}
          autoHideDuration={4000}
          onClose={() => setNotification(null)}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        >
          <Alert severity={notification?.type || 'info'} variant="filled" sx={{ width: '100%', borderRadius: 2 }}>
            {notification?.msg}
          </Alert>
        </Snackbar>
      </Box>
    </ThemeProvider>
  );
}
