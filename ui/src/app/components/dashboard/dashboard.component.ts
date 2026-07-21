import { Component, OnInit, OnDestroy, OnChanges, SimpleChanges, ViewChild, ElementRef, AfterViewInit, Output, EventEmitter, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '../../services/api.service';
import { ComponentStateService } from '../../services/component-state.service';
import { ThemeService } from '../../services/theme.service';

import { Chart, registerables } from 'chart.js';
import { DataTableComponent, TableColumn, PaginationConfig } from '../shared/data-table/data-table.component';

Chart.register(...registerables);

interface PhaseTiming {
  phase: string;
  duration_ms: number;
  duration_formatted: string;
  metadata?: any;
}

interface QueryLog {
  log_id: string;
  source: string;
  user_query: string;
  generated_sql: string | null;
  total_time_ms: number;
  total_time_formatted: string;
  phase_timings: PhaseTiming[];
  success: boolean;
  status_text: string;
  row_count: number;
  error_message: string | null;
  session_id: string | null;
  intent: string | null;
  confidence: string | null;
  tables_used: string[];
  timestamp: string;
  token_usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    llm_calls: number;
    estimated_cost?: number;
    model?: string;
  } | null;
}

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule, MatButtonModule, MatProgressSpinnerModule, MatTooltipModule, DataTableComponent],
  templateUrl: './dashboard.component.html',
  styleUrls: ['./dashboard.component.scss']
})
export class DashboardComponent implements OnInit, OnDestroy, OnChanges, AfterViewInit {
  @ViewChild('trendChart') trendChartRef!: ElementRef<HTMLCanvasElement>;
  @ViewChild('passFailChart') passFailChartRef!: ElementRef<HTMLCanvasElement>;
  @ViewChild('topTablesChart') topTablesChartRef!: ElementRef<HTMLCanvasElement>;
  @ViewChild('responseTimeChart') responseTimeChartRef!: ElementRef<HTMLCanvasElement>;
  @ViewChild('modelChart') modelChartRef!: ElementRef<HTMLCanvasElement>;

  @Output() openAnalytics = new EventEmitter<void>();
  @Output() openNewChat = new EventEmitter<void>();
  @Input() dbIdentity: string = '';
  @Input() sessionId: string | null = null;

  // Data — OpenAI Codex query logs
  logs: QueryLog[] = [];
  filteredLogs: QueryLog[] = [];
  paginatedLogs: QueryLog[] = [];
  loading = true;
  error: string | null = null;

  recalculating = false;

  // Filters
  searchTerm = '';

  // KPIs
  totalQueries = 0;
  successCount = 0;
  failedCount = 0;
  successRate = 0;
  avgTimeMs = 0;
  totalRows = 0;
  p95TimeMs = 0;
  fastestQueryMs = 0;
  slowestQueryMs = 0;

  // Token/Cost KPIs
  totalTokensUsed = 0;
  totalEstimatedCost = 0;
  avgTokensPerQuery = 0;
  queriesWithTokens = 0;
  avgCostPerQuery = 0;

  // Model usage
  modelUsage: { model: string; count: number; cost: number }[] = [];

  // Top tables
  topTables: { name: string; count: number }[] = [];

  // Table config
  tableColumns: TableColumn[] = [
    {
      key: 'status_text', label: 'Status', type: 'status', width: '70px', align: 'center',
      statusConfig: { successValue: 'success', successIcon: 'check_circle', failureIcon: 'cancel' }
    },
    { key: 'user_query', label: 'Query', type: 'text' },
    { key: 'row_count', label: 'Rows', type: 'number', width: '70px', align: 'right' },
    {
      key: 'total_time_formatted', label: 'Time', type: 'text', width: '90px', align: 'right',
      format: (val: string, row: any) => row?.total_time_ms > 5000 ? `⚠️ ${val}` : val
    },
    {
      key: 'token_display', label: 'Tokens', type: 'text', width: '90px', align: 'right',
      format: (val: string) => val || '—'
    },
    {
      key: 'model_display', label: 'Model', type: 'text', width: '120px',
      format: (val: string) => val || '—'
    },
    {
      key: 'cost_display', label: 'Cost', type: 'text', width: '80px', align: 'right',
      format: (val: string) => val || '—'
    },
    {
      key: 'timestamp', label: 'When', type: 'text', width: '140px',
      format: (val: string) => {
        const d = new Date(val);
        return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      }
    },
    {
      key: 'analyze', label: 'Analysis', type: 'actions', width: '80px', align: 'center',
      actionIcon: 'insights', actionTooltip: 'Analyze this query',
      actionVisible: (row: any) => !!row.generated_sql
    }
  ];

  tablePagination: PaginationConfig = {
    enabled: true, pageSize: 10, pageSizeOptions: [10, 25, 50], totalCount: 0, currentPage: 1
  };

  // Charts
  private trendChart: Chart | null = null;
  private passFailChart: Chart | null = null;
  private topTablesChart: Chart | null = null;
  private responseTimeChart: Chart | null = null;
  private modelChart: Chart | null = null;
  private refreshInterval: any = null;

  // Detail modal
  selectedLog: QueryLog | null = null;

  constructor(private apiService: ApiService, private componentState: ComponentStateService, private themeService: ThemeService) {}

  ngOnInit(): void {
    this.loadData();
    this.refreshInterval = setInterval(() => this.loadData(), 30000);

  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['dbIdentity'] && !changes['dbIdentity'].firstChange) {
      this.loadData();
    }
  }

  ngAfterViewInit(): void {
    // Charts are built after data loads
  }

  ngOnDestroy(): void {
    if (this.refreshInterval) clearInterval(this.refreshInterval);
    this.destroyCharts();
  }

  loadData(): void {
    this.loading = true;
    this.error = null;

    this.apiService.getCopilotExecutionLogs(10000).subscribe({
      next: (res) => {
        if (res.success && res.logs) {
          this.logs = res.logs.map((l: any) => ({
            ...l,
            status_text: l.success ? 'success' : 'failed',
            tables_used: l.tables_used || [],
            phase_timings: (l.phase_timings || []).map((p: any) => ({
              ...p,
              phase: p.phase || p.name || 'unknown',
              duration_ms: p.duration_ms || p.time_ms || 0,
              duration_formatted: p.duration_formatted || `${Math.round(p.duration_ms || p.time_ms || 0)}ms`,
            })),
            total_time_formatted: l.total_time_formatted || (l.total_time_ms >= 1000 ? `${(l.total_time_ms / 1000).toFixed(1)}s` : `${Math.round(l.total_time_ms)}ms`),
            error_message: l.error_message || l.error || null,
            token_usage: l.token_usage && l.token_usage.total_tokens > 0 ? l.token_usage : null,
            token_display: l.token_usage?.total_tokens ? this.formatTokenCount(l.token_usage.total_tokens) : '',
            model_display: l.token_usage?.model || l.model || '',
            cost_display: l.token_usage?.estimated_cost ? `$${l.token_usage.estimated_cost.toFixed(4)}` : '',
          }));
          this.computeKPIs();
          this.computeTopTables();
          this.applyFilters();
          setTimeout(() => this.buildCharts(), 50);
        } else {
          this.error = res.error || 'Failed to load data';
        }
        this.loading = false;
      },
      error: (err) => {
        this.error = err.message || 'Failed to load dashboard data';
        this.loading = false;
      }
    });
  }

  // ── Filters & Pagination ─────────────────────────────────────────
  applyFilters(): void {
    let result = [...this.logs];

    if (this.searchTerm.trim()) {
      const term = this.searchTerm.toLowerCase().trim();
      result = result.filter(l =>
        l.user_query.toLowerCase().includes(term) ||
        (l.generated_sql || '').toLowerCase().includes(term) ||
        (l.error_message || '').toLowerCase().includes(term)
      );
    }

    this.filteredLogs = result;
    this.tablePagination = {
      ...this.tablePagination,
      totalCount: this.filteredLogs.length,
      currentPage: 1
    };
    this.updatePaginatedLogs();
  }

  private updatePaginatedLogs(): void {
    const start = (this.tablePagination.currentPage - 1) * this.tablePagination.pageSize;
    const end = start + this.tablePagination.pageSize;
    this.paginatedLogs = this.filteredLogs.slice(start, end);
  }

  onPageChange(event: { page: number; pageSize: number }): void {
    this.tablePagination = {
      ...this.tablePagination,
      currentPage: event.page,
      pageSize: event.pageSize
    };
    this.updatePaginatedLogs();
  }

  clearFilters(): void {
    this.searchTerm = '';
    this.applyFilters();
  }

  get hasActiveFilters(): boolean {
    return !!this.searchTerm;
  }

  // ── KPIs ──────────────────────────────────────────────────────────
  private computeKPIs(): void {
    this.totalQueries = this.logs.length;
    this.successCount = this.logs.filter(l => l.success).length;
    this.failedCount = this.totalQueries - this.successCount;
    this.successRate = this.totalQueries > 0 ? (this.successCount / this.totalQueries) * 100 : 0;
    this.totalRows = this.logs.reduce((s, l) => s + (l.row_count || 0), 0);

    const times = this.logs.map(l => l.total_time_ms).filter(t => t > 0).sort((a, b) => a - b);
    this.avgTimeMs = times.length > 0 ? times.reduce((s, t) => s + t, 0) / times.length : 0;
    this.fastestQueryMs = times.length > 0 ? times[0] : 0;
    this.slowestQueryMs = times.length > 0 ? times[times.length - 1] : 0;
    this.p95TimeMs = times.length > 0 ? times[Math.floor(times.length * 0.95)] : 0;

    const logsWithTokens = this.logs.filter(l => l.token_usage?.total_tokens);
    this.queriesWithTokens = logsWithTokens.length;
    this.totalTokensUsed = logsWithTokens.reduce((s, l) => s + (l.token_usage?.total_tokens || 0), 0);
    this.totalEstimatedCost = logsWithTokens.reduce((s, l) => s + (l.token_usage?.estimated_cost || 0), 0);
    this.avgTokensPerQuery = this.queriesWithTokens > 0 ? Math.round(this.totalTokensUsed / this.queriesWithTokens) : 0;
    this.avgCostPerQuery = logsWithTokens.length > 0 ? this.totalEstimatedCost / logsWithTokens.length : 0;

    this.computeModelUsage();
  }

  private computeTopTables(): void {
    const counts: Record<string, number> = {};
    for (const log of this.logs) {
      for (const t of log.tables_used) {
        counts[t] = (counts[t] || 0) + 1;
      }
    }
    this.topTables = Object.entries(counts)
      .map(([name, count]) => ({ name, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 10);
  }

  private computeModelUsage(): void {
    const modelMap: Record<string, { count: number; cost: number }> = {};
    for (const log of this.logs) {
      if (!log.token_usage?.total_tokens) continue;
      const model = log.token_usage.model || (log as any).model_display || 'unknown';
      if (!modelMap[model]) modelMap[model] = { count: 0, cost: 0 };
      modelMap[model].count++;
      modelMap[model].cost += log.token_usage.estimated_cost || 0;
    }
    this.modelUsage = Object.entries(modelMap)
      .map(([model, v]) => ({ model, count: v.count, cost: v.cost }))
      .sort((a, b) => b.count - a.count);
  }

  // ── Charts ────────────────────────────────────────────────────────
  private destroyCharts(): void {
    this.trendChart?.destroy();
    this.passFailChart?.destroy();
    this.topTablesChart?.destroy();
    this.responseTimeChart?.destroy();
    this.modelChart?.destroy();
    this.trendChart = null;
    this.passFailChart = null;
    this.topTablesChart = null;
    this.responseTimeChart = null;
    this.modelChart = null;
  }

  private buildCharts(): void {
    this.destroyCharts();
    this.buildTrendChart();
    this.buildPassFailChart();
    this.buildTopTablesChart();
    this.buildResponseTimeChart();
    this.buildModelChart();
  }

  private buildTrendChart(): void {
    const ctx = this.trendChartRef?.nativeElement?.getContext('2d');
    if (!ctx) return;

    const sorted = [...this.logs].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
    if (!sorted.length) return;

    const firstDate = new Date(sorted[0].timestamp);
    const lastDate = new Date(sorted[sorted.length - 1].timestamp);
    const rangeHours = (lastDate.getTime() - firstDate.getTime()) / (1000 * 60 * 60);

    let bucketFn: (d: Date) => string;
    if (rangeHours <= 24) {
      bucketFn = d => `${d.getHours()}:00`;
    } else if (rangeHours <= 168) {
      bucketFn = d => {
        const h = Math.floor(d.getHours() / 6) * 6;
        return `${d.getMonth() + 1}/${d.getDate()} ${h}:00`;
      };
    } else if (rangeHours <= 720) {
      bucketFn = d => `${d.getMonth() + 1}/${d.getDate()}`;
    } else {
      bucketFn = d => {
        const weekStart = new Date(d);
        weekStart.setDate(d.getDate() - d.getDay());
        return `${weekStart.getMonth() + 1}/${weekStart.getDate()}`;
      };
    }

    const buckets = new Map<string, { success: number; failed: number; times: number[] }>();
    for (const log of sorted) {
      const key = bucketFn(new Date(log.timestamp));
      if (!buckets.has(key)) buckets.set(key, { success: 0, failed: 0, times: [] });
      const b = buckets.get(key)!;
      if (log.success) b.success++; else b.failed++;
      b.times.push(log.total_time_ms);
    }

    const labels = Array.from(buckets.keys());
    const successData = labels.map(k => buckets.get(k)!.success);
    const failedData = labels.map(k => buckets.get(k)!.failed);
    const avgTimeData = labels.map(k => {
      const t = buckets.get(k)!.times;
      return t.length ? t.reduce((s, v) => s + v, 0) / t.length / 1000 : 0;
    });

    const isDark = this.themeService?.isDarkMode;
    const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
    const tickColor = isDark ? '#94a3b8' : '#64748b';

    this.trendChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: 'Success',
            data: successData,
            backgroundColor: 'rgba(59,130,246,0.7)',
            borderRadius: 3,
            yAxisID: 'y',
            order: 2
          },
          {
            label: 'Failed',
            data: failedData,
            backgroundColor: 'rgba(239,68,68,0.75)',
            borderRadius: 3,
            yAxisID: 'y',
            order: 3
          },
          {
            label: 'Avg Time (s)',
            data: avgTimeData,
            type: 'line',
            borderColor: '#8b5cf6',
            backgroundColor: 'rgba(139,92,246,0.08)',
            fill: true,
            tension: 0.4,
            borderWidth: 2,
            pointRadius: 2,
            pointHoverRadius: 5,
            yAxisID: 'y1',
            order: 1
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'bottom',
            labels: { boxWidth: 10, boxHeight: 10, font: { size: 11 }, padding: 14, usePointStyle: true }
          },
          tooltip: {
            backgroundColor: isDark ? 'rgba(15,25,35,0.95)' : 'rgba(30,41,59,0.95)',
            titleFont: { size: 12 },
            bodyFont: { size: 11 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: (item: any) => {
                if (item.dataset.label === 'Avg Time (s)') return ` Avg Time: ${item.raw.toFixed(1)}s`;
                return ` ${item.dataset.label}: ${item.raw}`;
              }
            }
          }
        },
        scales: {
          y: {
            beginAtZero: true,
            stacked: true,
            title: { display: true, text: 'Queries', font: { size: 10, weight: 'bold' as any }, color: tickColor },
            grid: { color: gridColor },
            ticks: { font: { size: 10 }, color: tickColor }
          },
          y1: {
            position: 'right',
            beginAtZero: true,
            title: { display: true, text: 'Avg Time (s)', font: { size: 10, weight: 'bold' as any }, color: '#8b5cf6' },
            grid: { drawOnChartArea: false },
            ticks: { font: { size: 10 }, color: '#8b5cf6' }
          },
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { font: { size: 10 }, color: tickColor, maxRotation: 45, autoSkip: true, maxTicksLimit: 15 }
          }
        }
      }
    });
  }

  private buildPassFailChart(): void {
    const ctx = this.passFailChartRef?.nativeElement?.getContext('2d');
    if (!ctx) return;

    this.passFailChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Passed', 'Failed'],
        datasets: [{
          data: [this.successCount, this.failedCount],
          backgroundColor: ['#22c55e', '#ef4444'],
          borderWidth: 2,
          borderColor: '#ffffff'
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '65%',
        plugins: {
          legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 }, padding: 10 } }
        }
      }
    });
  }

  private buildTopTablesChart(): void {
    const ctx = this.topTablesChartRef?.nativeElement?.getContext('2d');
    if (!ctx || this.topTables.length === 0) return;

    const colors = ['#3b82f6', '#8b5cf6', '#06b6d4', '#f59e0b', '#ef4444', '#22c55e', '#ec4899', '#14b8a6', '#f97316', '#6366f1'];

    this.topTablesChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: this.topTables.map(t => t.name.length > 18 ? t.name.slice(0, 18) + '…' : t.name),
        datasets: [{
          label: 'Queries',
          data: this.topTables.map(t => t.count),
          backgroundColor: this.topTables.map((_, i) => colors[i % colors.length]),
          borderRadius: 4,
          barPercentage: 0.7
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, ticks: { font: { size: 10 } } },
          y: { ticks: { font: { size: 10 } } }
        }
      }
    });
  }

  private buildResponseTimeChart(): void {
    const ctx = this.responseTimeChartRef?.nativeElement?.getContext('2d');
    if (!ctx || !this.logs.length) return;

    const buckets = [
      { label: '< 1s', max: 1000 },
      { label: '1-3s', max: 3000 },
      { label: '3-5s', max: 5000 },
      { label: '5-10s', max: 10000 },
      { label: '10-30s', max: 30000 },
      { label: '> 30s', max: Infinity },
    ];
    const counts = buckets.map(() => 0);
    for (const log of this.logs) {
      const t = log.total_time_ms;
      for (let i = 0; i < buckets.length; i++) {
        if (t < buckets[i].max) { counts[i]++; break; }
      }
    }

    const isDark = this.themeService?.isDarkMode;
    const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
    const tickColor = isDark ? '#94a3b8' : '#64748b';
    const colors = ['#22c55e', '#3b82f6', '#8b5cf6', '#f59e0b', '#ef4444', '#dc2626'];

    this.responseTimeChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: buckets.map(b => b.label),
        datasets: [{
          label: 'Queries',
          data: counts,
          backgroundColor: colors,
          borderRadius: 4,
          barPercentage: 0.7
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: isDark ? 'rgba(15,25,35,0.95)' : 'rgba(30,41,59,0.95)',
            callbacks: {
              label: (item: any) => ` ${item.raw} queries (${((item.raw / this.logs.length) * 100).toFixed(1)}%)`
            }
          }
        },
        scales: {
          y: {
            beginAtZero: true,
            title: { display: true, text: 'Queries', font: { size: 10, weight: 'bold' as any }, color: tickColor },
            grid: { color: gridColor },
            ticks: { font: { size: 10 }, color: tickColor }
          },
          x: {
            grid: { display: false },
            ticks: { font: { size: 10 }, color: tickColor }
          }
        }
      }
    });
  }

  private buildModelChart(): void {
    const ctx = this.modelChartRef?.nativeElement?.getContext('2d');
    if (!ctx || this.modelUsage.length === 0) return;

    const colors = ['#3b82f6', '#8b5cf6', '#06b6d4', '#f59e0b', '#ef4444', '#22c55e', '#ec4899', '#14b8a6'];

    this.modelChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: this.modelUsage.map(m => m.model),
        datasets: [{
          data: this.modelUsage.map(m => m.count),
          backgroundColor: colors.slice(0, this.modelUsage.length),
          borderWidth: 2,
          borderColor: '#ffffff'
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '55%',
        plugins: {
          legend: { position: 'right', labels: { boxWidth: 10, font: { size: 10 }, padding: 8 } },
          tooltip: {
            callbacks: {
              label: (item: any) => {
                const m = this.modelUsage[item.dataIndex];
                return ` ${m.model}: ${m.count} queries ($${m.cost.toFixed(4)})`;
              }
            }
          }
        }
      }
    });
  }

  // ── Helpers ────────────────────────────────────────────────────────
  formatMs(ms: number): string {
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }

  formatNumber(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return n.toString();
  }

  onLogClick(log: QueryLog): void {
    this.selectedLog = log;
  }

  closeDetail(): void {
    this.selectedLog = null;
  }

  onAnalyzeQuery(log: QueryLog): void {
    if (!log.generated_sql) return;
    this.componentState.saveState('analytics-data', {
      data: [],
      columns: [],
      userQuery: log.user_query,
      sql: log.generated_sql,
      sessionId: this.sessionId || '',
      autoRun: true,
      phaseTimings: log.phase_timings,
      totalTimeMs: log.total_time_ms,
      totalTimeFormatted: log.total_time_formatted
    });
    this.selectedLog = null;
    this.openAnalytics.emit();
  }

  onTableAction(event: { row: any; columnKey: string }): void {
    if (event.columnKey === 'analyze') {
      this.onAnalyzeQuery(event.row);
    }
  }

  getPhasePercent(timing: PhaseTiming): number {
    if (!this.selectedLog) return 0;
    const total = this.selectedLog.total_time_ms;
    return total > 0 ? (timing.duration_ms / total) * 100 : 0;
  }

  getEstimatedCost(usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number; model?: string }): string {
    // Model-specific pricing per 1M tokens: [input, output]
    // ORDER MATTERS: more-specific keys must appear before shorter prefixes
    const pricing: Record<string, [number, number]> = {
      'gpt-5.5':              [5.00,  30.00],
      'gpt-5.4-mini':         [0.75,  4.50],
      'gpt-5.4-nano':         [0.20,  1.25],
      'gpt-5.4':              [2.50,  15.00],
      'gpt-5-nano':           [0.20,  1.25],
      'gpt-4.1-mini':         [0.40,  1.60],
      'gpt-4.1-nano':         [0.10,  0.40],
      'gpt-4.1':              [2.00,  8.00],
      'gpt-4o-mini':          [0.15,  0.60],
      'gpt-4o':               [2.50,  10.00],
      'gpt-4-turbo':          [10.00, 30.00],
      'gpt-4':                [30.00, 60.00],
      'gpt-3.5-turbo':        [0.50,  1.50],
      'o4-mini':              [1.10,  4.40],
      'o3-mini':              [1.10,  4.40],
      'o1-mini':              [3.00,  12.00],
      'o1':                   [15.00, 60.00],
    };
    const model = (usage.model || '').toLowerCase();
    let rates = pricing['gpt-4o-mini'];
    for (const [key, val] of Object.entries(pricing)) {
      if (model.includes(key)) { rates = val; break; }
    }
    const inputCost = (usage.prompt_tokens / 1_000_000) * rates[0];
    const outputCost = (usage.completion_tokens / 1_000_000) * rates[1];
    const total = inputCost + outputCost;
    if (total < 0.001) return `$${(total * 1000).toFixed(3)}m`;
    return `$${total.toFixed(4)}`;
  }

  refresh(): void {
    this.loadData();
  }

  recalculateCosts(): void {
    this.recalculating = true;
    this.apiService.recalculateCosts().subscribe({
      next: () => {
        this.recalculating = false;
        this.refresh();
      },
      error: (err: any) => {
        console.error('Recalculate costs failed:', err);
        this.recalculating = false;
      }
    });
  }

  /** Format token count: 1234 → "1.2K", 123456 → "123K" */
  formatTokenCount(count: number): string {
    if (!count) return '—';
    if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
    if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
    return `${count}`;
  }

  /** Format cost as dollar string */
  formatCost(cost: number): string {
    if (!cost) return '$0.00';
    if (cost < 0.01) return `$${cost.toFixed(4)}`;
    return `$${cost.toFixed(2)}`;
  }
}
