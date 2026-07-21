import { Component, Output, EventEmitter, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ThemeService } from '../../services/theme.service';

export type ViewType = 'dashboard' | 'configuration' | 'schema' | 'analytics' | 'mcp-agent' | 'knowledge';

@Component({
  selector: 'app-sidebar',
  standalone: true,
  imports: [CommonModule, MatIconModule, MatTooltipModule],
  templateUrl: './sidebar.component.html',
  styleUrl: './sidebar.component.scss'
})
export class SidebarComponent {
  @Output() viewChanged = new EventEmitter<ViewType>();
  @Output() disconnect = new EventEmitter<void>();
  @Input() connected: boolean = false;
  @Input() database: string = '';
  @Input() activeView: ViewType = 'mcp-agent';
  
  collapsed: boolean = false;

  navigationItems = [
    { id: 'mcp-agent' as ViewType, label: 'OpenAI Codex', icon: 'smart_toy', section: 'main' },
    { id: 'dashboard' as ViewType, label: 'Dashboard', icon: 'dashboard', section: 'main' },
    { id: 'schema' as ViewType, label: 'Schema Explorer', icon: 'account_tree', section: 'main' },
    { id: 'analytics' as ViewType, label: 'Analytics', icon: 'insights', section: 'tools' },
    { id: 'knowledge' as ViewType, label: 'Database Context', icon: 'description', section: 'tools' },
    { id: 'configuration' as ViewType, label: 'Settings', icon: 'settings', section: 'tools' },
  ];

  constructor(public themeService: ThemeService) {}

  selectView(view: ViewType): void {
    // Emit the view change - parent will update activeView which will flow back via Input
    this.viewChanged.emit(view);
  }

  onDisconnect(): void {
    this.disconnect.emit();
  }

  toggleCollapse(): void {
    this.collapsed = !this.collapsed;
  }
}
