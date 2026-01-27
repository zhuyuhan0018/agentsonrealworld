/**
 * JavaScript/Node.js HTTP Interceptor for http, https, fetch, and axios.
 * 
 * Usage:
 *   - For Node.js: require('./javascript_interceptor.js') at the start of your MCP server
 *   - For ES modules: import './javascript_interceptor.js' at the start
 * 
 * This will intercept all HTTP requests and log them via a Python logger process
 * or write to a shared log file.
 */

const fs = require('fs');
const path = require('path');
const http = require('http');
const https = require('https');
const { URL } = require('url');

// Save original fs methods BEFORE any interception happens
// This allows HTTP interceptor to write logs without triggering system interceptor recursion
// Note: These must be saved at module load time, before system_javascript_interceptor.js wraps them
const originalAppendFileSync = fs.appendFileSync;
const originalExistsSync = fs.existsSync;
const originalMkdirSync = fs.mkdirSync;

// Debug file logger - writes to /tmp/interceptor.log for troubleshooting
function debugLog(msg) {
    try {
        const timestamp = new Date().toISOString();
        originalAppendFileSync('/tmp/interceptor.log', `[${timestamp}] [js] ${msg}\n`, 'utf8');
    } catch (err) {
        // Ignore errors
    }
}

debugLog('http_javascript_interceptor.js module loading...');

// Load replay manager (if available)
let HTTPReplayManager = null;
try {
    HTTPReplayManager = require('./http_replay_manager.js');
} catch (err) {
    // Replay manager not available, continue without replay
}

// Get server name from environment
const SERVER_NAME = process.env.MCP_SERVER_NAME || process.env.SERVER_NAME || 'unknown';

// Lazy load replay manager instance (only if in replay mode)
let _replayManager = null;

function getReplayManager() {
    if (_replayManager !== null) {
        return _replayManager;
    }
    
    if (HTTPReplayManager && HTTPReplayManager.isReplayMode()) {
        const replayDir = '/mcp_logs';  // Now mounts entire workflow_logs
        _replayManager = HTTPReplayManager.createFromEnv(replayDir);
        if (_replayManager) {
            console.log(`[HTTPInterceptor] HTTP replay mode enabled for execution ${HTTPReplayManager.getReplayExecutionId()}`);
        }
    }
    
    return _replayManager;
}

// Check if in replay mode (skip logging, only replay responses)
function isReplayMode() {
    return process.env.HTTP_REPLAY_MODE !== undefined;
}

/**
 * Log an HTTP request to the unified replay log.
 * Only logs when workflow context (EXECUTION_ID, WORKFLOW_ID) is available.
 * Skips logging in replay mode (we're reading, not writing).
 */
function logRequest(logEntry) {
    const method = logEntry.request ? logEntry.request.method : 'UNKNOWN';
    const url = logEntry.request ? logEntry.request.url : 'UNKNOWN';
    debugLog(`logRequest called: ${method} ${url}`);
    
    // Skip logging in replay mode
    if (isReplayMode()) {
        debugLog('  Skipping (replay mode)');
        return;
    }
    
    const executionId = logEntry.execution_id || process.env.EXECUTION_ID;
    const workflowId = logEntry.workflow_id || process.env.WORKFLOW_ID;
    
    debugLog(`  executionId=${executionId}, workflowId=${workflowId}`);
    
    // Skip if no workflow context
    if (!executionId || !workflowId) {
        debugLog('  Skipping (no workflow context)');
        return;
    }
    
    try {
        // Unified log location: /mcp_logs/{workflow_id}/{execution_id}/http_replay/{server_name}/requests.jsonl
        const replayDir = process.env.HTTP_REPLAY_DIR || '/mcp_logs';
        const replayLogDir = path.join(replayDir, workflowId, executionId, 'http_replay', SERVER_NAME);
        const replayLogFile = path.join(replayLogDir, 'requests.jsonl');
        
        debugLog(`  Writing to ${replayLogFile}`);
        
        // Ensure directory exists
        if (!originalExistsSync(replayLogDir)) {
            originalMkdirSync(replayLogDir, { recursive: true });
        }
        
        // Write log entry
        const logLine = JSON.stringify(logEntry) + '\n';
        originalAppendFileSync(replayLogFile, logLine, 'utf8');
        debugLog('  Write successful');
    } catch (err) {
        debugLog(`  Write failed: ${err.message}`);
        console.error('Failed to write HTTP log:', err.message);
    }
}

/**
 * Serialize headers object to plain object.
 */
function serializeHeaders(headers) {
    const result = {};
    if (headers) {
        for (const [key, value] of Object.entries(headers)) {
            result[key] = String(value);
        }
    }
    return result;
}

/**
 * Get HTTP status text from status code.
 */
function getStatusText(statusCode) {
    const statusTexts = {
        200: 'OK',
        201: 'Created',
        204: 'No Content',
        400: 'Bad Request',
        401: 'Unauthorized',
        403: 'Forbidden',
        404: 'Not Found',
        500: 'Internal Server Error',
        502: 'Bad Gateway',
        503: 'Service Unavailable',
    };
    return statusTexts[statusCode] || 'Unknown';
}

// ===== Intercept http/https modules =====

const originalHttpRequest = http.request;
const originalHttpsRequest = https.request;

function wrapHttpRequest(originalRequest, protocol) {
    return function(options, callback) {
        const startTime = Date.now();
        const method = (typeof options === 'object' ? options.method : 'GET') || 'GET';
        debugLog(`[${protocol}] Intercepted ${method} request`);
        
        // Build full URL with query parameters
        let url;
        if (typeof options === 'string') {
            url = options;
        } else if (options.href) {
            url = options.href;
        } else {
            const host = options.hostname || options.host || 'localhost';
            const path = options.path || '/';
            url = `${protocol}//${host}${path}`;
        }
        
        // Extract request body
        let requestBody = null;
        if (options.body) {
            if (Buffer.isBuffer(options.body)) {
                requestBody = options.body.toString('utf8');
            } else if (typeof options.body === 'string') {
                requestBody = options.body;
            } else if (typeof options.body === 'object') {
                try {
                    requestBody = JSON.stringify(options.body);
                } catch (err) {
                    requestBody = String(options.body);
                }
            } else {
                requestBody = String(options.body);
            }
        }
        
        // Check replay mode before making request
        const replayManager = getReplayManager();
        if (replayManager) {
            try {
                const method = (options.method || 'GET').toUpperCase();
                const recordedResponse = replayManager.findResponse(method, url);
                
                if (recordedResponse) {
                    // Return recorded response - create a mock IncomingMessage
                    const { IncomingMessage } = http;
                    const mockResponse = new IncomingMessage(null);
                    mockResponse.statusCode = recordedResponse.status || 200;
                    mockResponse.headers = recordedResponse.headers || {};
                    mockResponse.url = url;
                    
                    // Set response body
                    const responseBody = recordedResponse.body || '';
                    if (responseBody) {
                        const bodyBuffer = Buffer.from(responseBody, 'utf8');
                        mockResponse.push(bodyBuffer);
                        mockResponse.push(null); // End of stream
                    } else {
                        mockResponse.push(null);
                    }
                    
                    // Don't log during replay - we're reading, not recording
                    
                    // Call callback with mock response
                    if (callback) {
                        callback(mockResponse);
                    }
                    
                    return mockResponse;
                } else {
                    // No matching response found - throw error
                    const executionId = process.env.HTTP_REPLAY_MODE || process.env.EXECUTION_ID || 'unknown';
                    throw new Error(
                        `No matching response found for ${method} ${url} ` +
                        `in replay execution ${executionId}. ` +
                        `Replay mode requires all HTTP requests to have recorded responses.`
                    );
                }
            } catch (err) {
                // If it's our replay error, re-throw it
                if (err.message && err.message.includes('No matching response found')) {
                    throw err;
                }
                // Otherwise, log and continue with real request
                console.error(`[HTTPInterceptor] Replay check failed: ${err.message}`);
            }
        }
        
        // Create a wrapper for the response
        const wrappedCallback = function(res) {
            const chunks = [];
            const originalOnData = res.on.bind(res);
            
            res.on = function(event, handler) {
                if (event === 'data') {
                    return originalOnData(event, function(chunk) {
                        chunks.push(chunk);
                        if (handler) handler(chunk);
                    });
                }
                return originalOnData(event, handler);
            };
            
            res.once('end', function() {
                const durationMs = Date.now() - startTime;
                const responseBody = Buffer.concat(chunks).toString('utf8');
                
                // Get final URL from response if available (after redirects)
                const finalUrl = res.headers.location ? res.headers.location : url;
                
                logRequest({
                    timestamp: new Date().toISOString(),
                    language: 'javascript',
                    server: SERVER_NAME,
                    request: {
                        method: (options.method || 'GET').toUpperCase(),
                        url: url,
                        headers: serializeHeaders(options.headers || {}),
                        body: requestBody,
                    },
                    response: {
                        status: res.statusCode,
                        headers: serializeHeaders(res.headers),
                        body: responseBody,
                    },
                    duration_ms: durationMs,
                    execution_id: process.env.EXECUTION_ID,
                    workflow_id: process.env.WORKFLOW_ID,
                });
            });
            
            if (callback) callback(res);
        };
        
        const req = originalRequest.call(this, options, wrappedCallback);
        
        // Log request on error
        req.on('error', function(err) {
            const durationMs = Date.now() - startTime;
            logRequest({
                timestamp: new Date().toISOString(),
                language: 'javascript',
                server: SERVER_NAME,
                request: {
                    method: (options.method || 'GET').toUpperCase(),
                    url: url,
                    headers: serializeHeaders(options.headers || {}),
                    body: requestBody,
                },
                error: err.message,
                duration_ms: durationMs,
                execution_id: process.env.EXECUTION_ID,
                workflow_id: process.env.WORKFLOW_ID,
            });
        });
        
        return req;
    };
}

http.request = wrapHttpRequest(originalHttpRequest, 'http:');
https.request = wrapHttpRequest(originalHttpsRequest, 'https:');

// ===== Intercept fetch (Node.js 18+) =====

if (typeof global.fetch !== 'undefined') {
    const originalFetch = global.fetch;
    
    global.fetch = async function(url, options = {}) {
        const startTime = Date.now();
        const method = (options.method || 'GET').toUpperCase();
        const headers = serializeHeaders(options.headers || {});
        const requestUrl = String(url);
        
        // Extract request body
        let requestBody = null;
        if (options.body) {
            if (typeof options.body === 'string') {
                requestBody = options.body;
            } else if (Buffer.isBuffer(options.body)) {
                requestBody = options.body.toString('utf8');
            } else if (options.body instanceof FormData) {
                requestBody = '[FormData]';
            } else if (options.body instanceof URLSearchParams) {
                requestBody = options.body.toString();
            } else if (typeof options.body === 'object') {
                try {
                    requestBody = JSON.stringify(options.body);
                } catch (err) {
                    requestBody = String(options.body);
                }
            } else {
                requestBody = String(options.body);
            }
        }
        
        // Check replay mode before making request
        const replayManager = getReplayManager();
        if (replayManager) {
            try {
                const recordedResponse = replayManager.findResponse(method, requestUrl);
                
                if (recordedResponse) {
                    // Return recorded response - create a mock Response object
                    const mockResponse = new Response(recordedResponse.body || '', {
                        status: recordedResponse.status || 200,
                        statusText: getStatusText(recordedResponse.status || 200),
                        headers: recordedResponse.headers || {},
                    });
                    
                    // Override url property
                    Object.defineProperty(mockResponse, 'url', {
                        value: requestUrl,
                        writable: false
                    });
                    
                    // Don't log during replay - we're reading, not recording
                    
                    return mockResponse;
                } else {
                    // No matching response found - throw error
                    const executionId = process.env.HTTP_REPLAY_MODE || process.env.EXECUTION_ID || 'unknown';
                    throw new Error(
                        `No matching response found for ${method} ${requestUrl} ` +
                        `in replay execution ${executionId}. ` +
                        `Replay mode requires all HTTP requests to have recorded responses.`
                    );
                }
            } catch (err) {
                // If it's our replay error, re-throw it
                if (err.message && err.message.includes('No matching response found')) {
                    throw err;
                }
                // Otherwise, log and continue with real request
                console.error(`[HTTPInterceptor] Replay check failed: ${err.message}`);
            }
        }
        
        try {
            const response = await originalFetch(url, options);
            const durationMs = Date.now() - startTime;
            
            // Get final URL (after redirects)
            const finalUrl = response.url || requestUrl;
            
            // Clone response to read body without consuming it
            const responseClone = response.clone();
            let responseBody = null;
            try {
                const contentType = response.headers.get('content-type') || '';
                if (contentType.includes('application/json')) {
                    responseBody = JSON.stringify(await responseClone.json());
                } else {
                    responseBody = await responseClone.text();
                }
            } catch (err) {
                // Ignore errors reading response body
            }
            
            logRequest({
                timestamp: new Date().toISOString(),
                language: 'javascript',
                server: SERVER_NAME,
                request: {
                    method: method,
                    url: finalUrl,
                    headers: headers,
                    body: requestBody,
                },
                response: {
                    status: response.status,
                    headers: serializeHeaders(Object.fromEntries(response.headers.entries())),
                    body: responseBody,
                },
                duration_ms: durationMs,
                execution_id: process.env.EXECUTION_ID,
                workflow_id: process.env.WORKFLOW_ID,
            });
            
            return response;
        } catch (err) {
            const durationMs = Date.now() - startTime;
            logRequest({
                timestamp: new Date().toISOString(),
                language: 'javascript',
                server: SERVER_NAME,
                request: {
                    method: method,
                    url: requestUrl,
                    headers: headers,
                    body: requestBody,
                },
                error: err.message,
                duration_ms: durationMs,
                execution_id: process.env.EXECUTION_ID,
                workflow_id: process.env.WORKFLOW_ID,
            });
            throw err;
        }
    };
}

// ===== Intercept axios =====

try {
    const axios = require('axios');
    
    // Intercept requests
    axios.interceptors.request.use(function(config) {
        config._interceptor_startTime = Date.now();
        
        // Check replay mode before making request
        const replayManager = getReplayManager();
        if (replayManager) {
            try {
                const method = (config.method || 'GET').toUpperCase();
                const url = config.url || '';
                const recordedResponse = replayManager.findResponse(method, url);
                
                if (recordedResponse) {
                    // Return recorded response - create a mock axios response
                    const mockResponse = {
                        status: recordedResponse.status || 200,
                        statusText: getStatusText(recordedResponse.status || 200),
                        headers: recordedResponse.headers || {},
                        data: recordedResponse.body ? (() => {
                            try {
                                return JSON.parse(recordedResponse.body);
                            } catch {
                                return recordedResponse.body;
                            }
                        })() : '',
                        config: config,
                        request: {},
                    };
                    
                    // Don't log during replay - we're reading, not recording
                    
                    // Return resolved promise with mock response
                    return Promise.resolve(mockResponse);
                } else {
                    // No matching response found - throw error
                    const executionId = process.env.HTTP_REPLAY_MODE || process.env.EXECUTION_ID || 'unknown';
                    return Promise.reject(new Error(
                        `No matching response found for ${method} ${url} ` +
                        `in replay execution ${executionId}. ` +
                        `Replay mode requires all HTTP requests to have recorded responses.`
                    ));
                }
            } catch (err) {
                // If it's our replay error, re-throw it
                if (err.message && err.message.includes('No matching response found')) {
                    return Promise.reject(err);
                }
                // Otherwise, log and continue with real request
                console.error(`[HTTPInterceptor] Replay check failed: ${err.message}`);
            }
        }
        
        return config;
    });
    
    axios.interceptors.response.use(
        function(response) {
            const durationMs = Date.now() - (response.config._interceptor_startTime || Date.now());
            
            logRequest({
                timestamp: new Date().toISOString(),
                language: 'javascript',
                server: SERVER_NAME,
                request: {
                    method: (response.config.method || 'GET').toUpperCase(),
                    url: response.config.url,
                    headers: serializeHeaders(response.config.headers || {}),
                    body: response.config.data ? (typeof response.config.data === 'string' ? response.config.data : JSON.stringify(response.config.data)) : null,
                },
                response: {
                    status: response.status,
                    headers: serializeHeaders(response.headers || {}),
                    body: typeof response.data === 'string' ? response.data : JSON.stringify(response.data),
                },
                duration_ms: durationMs,
                execution_id: process.env.EXECUTION_ID,
                workflow_id: process.env.WORKFLOW_ID,
            });
            
            return response;
        },
        function(error) {
            const durationMs = Date.now() - (error.config?._interceptor_startTime || Date.now());
            
            logRequest({
                timestamp: new Date().toISOString(),
                language: 'javascript',
                server: SERVER_NAME,
                request: {
                    method: (error.config?.method || 'GET').toUpperCase(),
                    url: error.config?.url || 'unknown',
                    headers: serializeHeaders(error.config?.headers || {}),
                    body: error.config?.data ? (typeof error.config.data === 'string' ? error.config.data : JSON.stringify(error.config.data)) : null,
                },
                error: error.message,
                duration_ms: durationMs,
                execution_id: process.env.EXECUTION_ID,
                workflow_id: process.env.WORKFLOW_ID,
            });
            
            return Promise.reject(error);
        }
    );
} catch (err) {
    // axios not installed, skip
}

// Log that interceptor is installed (only in recording mode)
debugLog(`Interceptor setup complete. EXECUTION_ID=${process.env.EXECUTION_ID}, WORKFLOW_ID=${process.env.WORKFLOW_ID}, SERVER_NAME=${SERVER_NAME}`);
if (!isReplayMode() && process.env.EXECUTION_ID && process.env.WORKFLOW_ID) {
    debugLog('Logging SETUP request...');
    logRequest({
        timestamp: new Date().toISOString(),
        language: 'javascript',
        server: SERVER_NAME,
        request: {
            method: 'SETUP',
            url: 'interceptor',
            headers: {},
            body: 'JavaScript HTTP interceptors initialized',
        },
        execution_id: process.env.EXECUTION_ID,
        workflow_id: process.env.WORKFLOW_ID,
    });
} else {
    debugLog(`Skipping SETUP log: isReplayMode=${isReplayMode()}, EXECUTION_ID=${process.env.EXECUTION_ID}, WORKFLOW_ID=${process.env.WORKFLOW_ID}`);
}

console.log('[HTTP Interceptor] JavaScript HTTP interceptors initialized' + 
    (isReplayMode() ? ' (replay mode)' : ''));


