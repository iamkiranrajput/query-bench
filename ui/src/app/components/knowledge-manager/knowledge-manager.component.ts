/** Database-scoped context-file manager for the Codex SQL agent. */

import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Component, Input, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { environment } from '../../../environments/environment';

interface ContextDocument {
  id: string;
  filename: string;
  database: string;
  size: number;
  uploaded_at: string;
}

@Component({
  selector: 'app-knowledge-manager',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule],
  templateUrl: './knowledge-manager.component.html',
  styleUrls: ['./knowledge-manager.component.scss'],
})
export class KnowledgeManagerComponent implements OnInit {
  @Input() sessionId = '';
  private readonly apiUrl = environment.apiUrl;

  error: string | null = null;
  notice: string | null = null;
  contextDocuments: ContextDocument[] = [];
  contextDatabaseName = '';
  contextUploading = false;

  constructor(private http: HttpClient) {}

  ngOnInit(): void {
    this.refreshContext();
  }

  refreshContext(): void {
    const params: Record<string, string> = {};
    if (this.sessionId) params['session_id'] = this.sessionId;
    if (this.contextDatabaseName.trim()) params['database_name'] = this.contextDatabaseName.trim();
    this.error = null;
    this.http.get<{ documents: ContextDocument[] }>(`${this.apiUrl}/api/database-context`, { params }).subscribe({
      next: (res) => this.contextDocuments = res.documents || [],
      error: (err) => this.error = err?.error?.detail || 'Failed to load database context files.',
    });
  }

  uploadContext(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    const form = new FormData();
    form.append('file', file);
    form.append('session_id', this.sessionId || '');
    form.append('database_name', this.contextDatabaseName.trim());
    this.contextUploading = true;
    this.error = null;
    this.notice = null;

    this.http.post<{ success: boolean }>(`${this.apiUrl}/api/database-context`, form).subscribe({
      next: () => {
        this.contextUploading = false;
        input.value = '';
        this.notice = `Uploaded ${file.name}. Codex will use it on the next answer.`;
        this.refreshContext();
      },
      error: (err) => {
        this.contextUploading = false;
        this.error = err?.error?.detail || 'Context upload failed.';
      },
    });
  }

  removeContext(doc: ContextDocument): void {
    this.error = null;
    this.http.delete(`${this.apiUrl}/api/database-context/${encodeURIComponent(doc.id)}`).subscribe({
      next: () => {
        this.notice = `Deleted ${doc.filename}.`;
        this.refreshContext();
      },
      error: (err) => this.error = err?.error?.detail || 'Delete failed.',
    });
  }
}
