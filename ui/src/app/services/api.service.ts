/**
 * API Service - Backend communication
 * With request cancellation for better performance
 */

import { Injectable, OnDestroy } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Observable, throwError, Subject } from 'rxjs';
import { catchError, takeUntil } from 'rxjs/operators';
import { environment } from '../../environments/environment';
import {
  ConnectRequest,
  ConnectResponse,
  DisconnectRequest,
  DisconnectResponse,
  HealthResponse
} from '../models/api.models';

@Injectable({
  providedIn: 'root'
})
export class ApiService implements OnDestroy {
  private apiUrl = environment.apiUrl;
  
  // Subjects for cancelling in-flight requests
  private queryCancel$ = new Subject<void>();
  private destroy$ = new Subject<void>();

  constructor(private http: HttpClient) {}

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.queryCancel$.complete();
  }

  /**
   * Cancel any in-flight query requests
   * Call this before starting a new query to prevent duplicate requests
   */
  cancelPendingQueries(): void {
    this.queryCancel$.next();
  }

  /**
   * Connect to database
   */
  connect(config: ConnectRequest): Observable<ConnectResponse> {
    return this.http.post<ConnectResponse>(
      `${this.apiUrl}/api/connect`,
      config
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Execute query to get SQL only (for query preview)
   * Automatically cancels previous in-flight requests
   */
  executeQuery(sessionId: string, prompt: string, bypassCache: boolean = false): Observable<any> {
    // Cancel any pending query
    this.queryCancel$.next();
    
    return this.http.post<any>(
      `${this.apiUrl}/api/query`,
      {
        session_id: sessionId,
        prompt: prompt,
        username: 'user',
        bypass_cache: bypassCache
      }
    ).pipe(
      takeUntil(this.queryCancel$),
      takeUntil(this.destroy$),
      catchError(this.handleError)
    );
  }

  /**
   * Execute SQL directly (for edited queries) with pagination support
   * @param useSqlLimit - If true, respect SQL's LIMIT clause; if false, strip it and apply pagination params
   * @param sessionId - Optional session ID to use specific database connection
   */
  /**
   * Submit user feedback for a query
   */
  submitFeedback(
    queryId: number | null,
    sessionId: string,
    userPrompt: string,
    generatedSql: string,
    feedbackType: 'up' | 'down',
    correctionSql?: string
  ): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/feedback`,
      {
        query_id: queryId,
        session_id: sessionId,
        user_prompt: userPrompt,
        generated_sql: generatedSql,
        feedback_type: feedbackType,
        correction_sql: correctionSql
      }
    ).pipe(
      catchError(this.handleError)
    );
  }

  executeDirectSQL(sql: string, limit: number = 10, offset: number = 0, useSqlLimit: boolean = true, sessionId: string | null = null): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/execute-sql`,
      { 
        sql: sql,
        limit: limit,
        offset: offset,
        use_sql_limit: useSqlLimit,
        session_id: sessionId
      }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Disconnect from database
   */
  disconnect(sessionId: string): Observable<DisconnectResponse> {
    const request: DisconnectRequest = {
      session_id: sessionId
    };

    return this.http.post<DisconnectResponse>(
      `${this.apiUrl}/api/disconnect`,
      request
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * List available databases on a server
   */
  listDatabases(hostname: string, port: number, username: string, password: string, dbType: string = 'postgresql'): Observable<any> {
    const request = {
      hostname,
      port,
      username,
      password,
      db_type: dbType
    };

    return this.http.post<any>(
      `${this.apiUrl}/api/list-databases`,
      request
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get table names for autocomplete suggestions (cached)
   */
  getTables(sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/get-tables`,
      { session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Health check
   */
  healthCheck(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(
      `${this.apiUrl}/api/health`
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get preset database connection metadata (no password returned)
   */
  getPresetConnection(): Observable<any> {
    return this.http.get<any>(
      `${this.apiUrl}/api/preset-connection`
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Auto-connect using server-side preset credentials.
   * Password never leaves the server.
   */
  connectPreset(): Observable<ConnectResponse> {
    return this.http.post<ConnectResponse>(
      `${this.apiUrl}/api/preset-connect`,
      {}
    ).pipe(
      catchError(this.handleError)
    );
  }

  // ============================================
  // AI Enhancement Endpoints
  // ============================================

  /**
   * Explain SQL query in plain English
   */
  explainQuery(sql: string, sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/explain-query`,
      { sql_query: sql, session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get database schema with AI-powered descriptions
   */
  getSchemaExplorer(sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/schema-explorer`,
      { session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get full visual schema structure with tables, columns, types, PKs, FKs, and relationships
   */
  getSchemaVisual(sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/schema-visual`,
      { session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Force-regenerate FK graph (rediscovers explicit + inferred relationships)
   */
  refreshFkGraph(sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/schema-visual-refresh`,
      { session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }


  /**
   * Fix failed SQL query using AI
   */
  fixQuery(sql: string, errorMessage: string, sessionId: string, userIntent?: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/fix-query`,
      { 
        original_query: sql, 
        error_message: errorMessage, 
        session_id: sessionId,
        user_intent: userIntent 
      }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get AI-powered description for a specific table
   */
  describeTable(tableName: string, sessionId: string): Observable<any> {
    return this.http.post<any>(
      `${this.apiUrl}/api/describe-table`,
      { table_name: tableName, session_id: sessionId }
    ).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Get schema intelligence status (embeddings ready/initializing)
   */
  getSchemaIntelligenceStatus(sessionId: string): Observable<any> {
    return this.http.get<any>(
      `${this.apiUrl}/api/schema-intelligence/status?session_id=${sessionId}`
    ).pipe(
      catchError(this.handleError)
    );
  }

  // ====================================================================
  // Execution Log Endpoints (OpenAI Codex agent)
  // ====================================================================

  getCopilotExecutionLogs(limit: number = 1000): Observable<any> {
    return this.http.get<any>(`${this.apiUrl}/api/copilot/logs?limit=${limit}`).pipe(
      catchError(this.handleError)
    );
  }

  recalculateCosts(): Observable<any> {
    return this.http.post<any>(`${this.apiUrl}/api/copilot/recalculate-costs`, {}).pipe(
      catchError(this.handleError)
    );
  }

  /**
   * Handle HTTP errors with meaningful messages
   */
  private handleError(error: HttpErrorResponse) {
    let errorMessage = 'An unknown error occurred';

    if (error.error instanceof ErrorEvent) {
      // Client-side error
      errorMessage = `Error: ${error.error.message}`;
    } else if (error.status === 0) {
      // Network error - server not reachable
      errorMessage = 'Unable to connect to the server. Please check if the backend is running.';
    } else if (error.status === 422) {
      // Validation error
      errorMessage = error.error?.error || error.error?.detail || 'Invalid request data. Please check your input.';
    } else if (error.status === 404) {
      errorMessage = 'The requested resource was not found.';
    } else if (error.status === 500) {
      // Server error - use friendly message from backend
      errorMessage = error.error?.error || error.error?.detail || 'An internal server error occurred. Please try again.';
    } else {
      // Other server errors
      errorMessage = error.error?.error || error.error?.message || error.message || `Server error (${error.status})`;
    }

    console.error('API Error:', errorMessage, error);
    return throwError(() => new Error(errorMessage));
  }
}
