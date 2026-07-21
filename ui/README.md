# Query Bench — Frontend UI

Angular 17 Material Design UI for AI-powered natural language database interaction.

## Quick Start

```bash
# Install dependencies
npm install

# Start development server
npm start
```

Application runs at: `http://localhost:1111`

Backend is expected at `http://localhost:2222`. The UI uses this explicit
localhost URL from `src/environments/environment.ts`.

### Stopping the Dev Server
```bash
# Press Ctrl+C in the terminal to stop the dev server
```

## Project Structure

```
ui/
├── src/
│   └── app/
│       ├── components/
│       │   ├── chat-query/          # Main chat interface for NL queries
│       │   ├── connection-dialog/   # Database connection form
│       │   ├── dashboard/           # Query logs dashboard
│       │   ├── data-analytics/      # Data visualization & column statistics
│       │   ├── results-panel/       # Query results table with export
│       │   ├── schema-explorer/     # Database schema browser
│       │   ├── history-panel/       # Query history with re-run
│       │   ├── chart-visualization/ # Chart rendering
│       │   ├── chat-panel/          # Chat message display
│       │   ├── sidebar/             # Navigation sidebar
│       │   ├── thinking-panel/      # Query processing status
│       │   └── shared/              # Shared components
│       ├── models/                  # TypeScript interfaces
│       └── services/
│           ├── api.service.ts       # Backend API client
│           ├── chat.service.ts      # Chat state management
│           ├── theme.service.ts     # Dark/light theme toggle
│           └── component-state.service.ts  # Cross-component state
├── angular.json                     # Angular configuration
├── package.json                     # Dependencies
├── tailwind.config.js               # Tailwind CSS configuration
└── tsconfig.json                    # TypeScript config
```

## Features

### Pages & Views
- **AI Assistant** — Chat interface for natural language queries with intent detection
- **Dashboard** — Query logs with timing breakdowns, success rates, and re-run capability
- **Data Analytics** — Visualizations (bar, line, pie, area charts), column statistics, and data export
- **Schema Explorer** — Browse database tables, columns, types, and relationships

### Components

#### Chat Query
- Natural language input with auto-suggestions
- Multi-intent support (new query, refine, sort, filter, paginate)
- Conversation history with session memory
- SQL preview with syntax highlighting
- Thinking/processing status indicator

#### Connection Dialog
- Material form with validation
- Database credentials input (host, port, database, username, password)
- Password visibility toggle

#### Results Panel
- Dynamic Material table with sortable columns
- CSV and Excel download
- Row count and execution time display
- Loading spinner and empty/error states

#### Data Analytics
- Chart type selection (bar, horizontal bar, line, area, pie, doughnut)
- Column and value axis selection
- Color theme picker (blue, green, purple, rainbow)
- Column statistics table (non-null count, unique, min, max, avg, top values)
- Editable SQL with re-run capability
- Query execution timing breakdown chart

#### Dashboard
- Query execution history log
- Phase timing visualization (SQL generation, execution)
- Re-run queries and open in analytics

#### Schema Explorer
- Table listing with row counts
- Column details (name, type, nullable)
- Search/filter tables

### Navigation
- Collapsible sidebar with icons
- Dark/light mode toggle
- Connection status indicator

## Configuration

### Environment Settings

**Development** (`src/environments/environment.ts`):
```typescript
export const environment = {
  production: false,
  apiUrl: 'http://localhost:2222'
};
```

**Production** (`src/environments/environment.prod.ts`):
```typescript
export const environment = {
  production: true,
  apiUrl: 'https://your-production-api.com'
};
```

## Tech Stack

- **Angular 17** with standalone components
- **Angular Material** — UI component library
- **Tailwind CSS** — Utility-first styling
- **Chart.js** — Data visualization
- **RxJS** — Reactive state management
- **TypeScript 5.4+**

**For backend setup, see [server/README.md](../server/README.md)**
