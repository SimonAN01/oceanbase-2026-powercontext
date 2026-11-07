/**
 * seekdb integration - Real seekdb 1.4.0 database layer
 * Uses SeekdbClient with SQL queries and full-text search
 */

import { SeekdbClient } from 'seekdb';

class SeekDB {
  #client;
  #dbPath;

  constructor({ dbPath = './.seekdb', database = 'test' } = {}) {
    this.#dbPath = dbPath;
    this.#client = new SeekdbClient({ path: dbPath, database });
  }

  async init() {
    // Create main experiments table with separate columns for filtering
    await this.#client.execute(`
      CREATE TABLE IF NOT EXISTS experiments (
        id VARCHAR(80) PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        dataset VARCHAR(255) NOT NULL,
        split VARCHAR(255) NOT NULL,
        metric VARCHAR(255) NOT NULL,
        value DOUBLE NOT NULL,
        status VARCHAR(50) NOT NULL,
        config TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    `);

    // Create full-text index for keyword search
    // Using space parser for basic tokenization (works for both English and Chinese)
    try {
      await this.#client.execute(`
        CREATE FULLTEXT INDEX IF NOT EXISTS idx_search
        ON experiments(name, config, notes)
        WITH PARSER space
      `);
    } catch (err) {
      // Index may already exist, which is fine
      if (!err.message.includes('already exists') && !err.message.includes('Duplicate')) {
        console.warn('Full-text index creation warning:', err.message);
      }
    }

    return this;
  }

  async insert(record) {
    const id = crypto.randomUUID();
    const { name, dataset, split, metric, value, status = 'completed', config = '', notes = '' } = record;

    await this.#client.execute(
      `INSERT INTO experiments (id, name, dataset, split, metric, value, status, config, notes)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [id, name, dataset, split, metric, value, status, config, notes]
    );

    return { _id: id, name, dataset, split, metric, value, status, config, notes, _createdAt: new Date().toISOString() };
  }

  async findAll() {
    const rows = await this.#client.execute('SELECT * FROM experiments ORDER BY created_at DESC');
    return rows.map((r) => ({
      _id: r.id,
      name: r.name,
      dataset: r.dataset,
      split: r.split,
      metric: r.metric,
      value: r.value,
      status: r.status,
      config: r.config,
      notes: r.notes,
      _createdAt: r.created_at,
    }));
  }

  async findById(id) {
    const rows = await this.#client.execute('SELECT * FROM experiments WHERE id = ?', [id]);
    if (rows.length === 0) return null;
    const r = rows[0];
    return {
      _id: r.id,
      name: r.name,
      dataset: r.dataset,
      split: r.split,
      metric: r.metric,
      value: r.value,
      status: r.status,
      config: r.config,
      notes: r.notes,
      _createdAt: r.created_at,
    };
  }

  /**
   * Full-text keyword search using MATCH AGAINST
   */
  async search(keywords) {
    return this.query({ keywords });
  }

  /**
   * Filter by exact field matches
   */
  async filter({ dataset, split, metric, status } = {}) {
    const conditions = [];
    const values = [];

    if (dataset) {
      conditions.push('dataset = ?');
      values.push(dataset);
    }
    if (split) {
      conditions.push('split = ?');
      values.push(split);
    }
    if (metric) {
      conditions.push('metric = ?');
      values.push(metric);
    }
    if (status) {
      conditions.push('status = ?');
      values.push(status);
    }

    let query = 'SELECT * FROM experiments';
    if (conditions.length > 0) {
      query += ' WHERE ' + conditions.join(' AND ');
    }
    query += ' ORDER BY created_at DESC';

    const rows = await this.#client.execute(query, values);
    return rows.map((r) => ({
      _id: r.id,
      name: r.name,
      dataset: r.dataset,
      split: r.split,
      metric: r.metric,
      value: r.value,
      status: r.status,
      config: r.config,
      notes: r.notes,
      _createdAt: r.created_at,
    }));
  }

  /**
   * Combined search + filter
   */
  async query({ keywords, dataset, split, metric, status } = {}) {
    const conditions = [];
    const values = [];

    // Full-text search condition
    const term = keywords?.trim();
    if (term && /\p{Script=Han}/u.test(term)) {
      conditions.push('(INSTR(name, ?) > 0 OR INSTR(config, ?) > 0 OR INSTR(notes, ?) > 0)');
      values.push(term, term, term);
    } else if (term) {
      conditions.push('MATCH(name, config, notes) AGAINST (? IN NATURAL LANGUAGE MODE)');
      values.push(term);
    }

    // Field filters
    if (dataset) {
      conditions.push('dataset = ?');
      values.push(dataset);
    }
    if (split) {
      conditions.push('split = ?');
      values.push(split);
    }
    if (metric) {
      conditions.push('metric = ?');
      values.push(metric);
    }
    if (status) {
      conditions.push('status = ?');
      values.push(status);
    }

    let query = 'SELECT * FROM experiments';
    if (conditions.length > 0) {
      query += ' WHERE ' + conditions.join(' AND ');
    }
    query += ' ORDER BY created_at DESC';

    const rows = await this.#client.execute(query, values);
    return rows.map((r) => ({
      _id: r.id,
      name: r.name,
      dataset: r.dataset,
      split: r.split,
      metric: r.metric,
      value: r.value,
      status: r.status,
      config: r.config,
      notes: r.notes,
      _createdAt: r.created_at,
    }));
  }

  /**
   * Find comparable records: same dataset, split, metric, and both completed
   */
  async findComparable(recordId) {
    const record = await this.findById(recordId);
    if (!record || record.status !== 'completed') return [];

    const rows = await this.#client.execute(
      `SELECT * FROM experiments
       WHERE id != ? AND dataset = ? AND split = ? AND metric = ? AND status = 'completed'
       ORDER BY created_at DESC`,
      [recordId, record.dataset, record.split, record.metric]
    );

    return rows.map((r) => ({
      _id: r.id,
      name: r.name,
      dataset: r.dataset,
      split: r.split,
      metric: r.metric,
      value: r.value,
      status: r.status,
      config: r.config,
      notes: r.notes,
      _createdAt: r.created_at,
    }));
  }

  async count() {
    const rows = await this.#client.execute('SELECT COUNT(*) as count FROM experiments');
    return rows[0].count;
  }

  async clear() {
    await this.#client.execute('DELETE FROM experiments');
  }

  async getVersion() {
    const rows = await this.#client.execute('SELECT VERSION() as version');
    return rows[0].version;
  }

  async getStats() {
    const all = await this.findAll();
    const datasets = [...new Set(all.map((r) => r.dataset))];
    const splits = [...new Set(all.map((r) => r.split))];
    const metrics = [...new Set(all.map((r) => r.metric))];
    return {
      total: all.length,
      datasets,
      splits,
      metrics,
    };
  }
}

export default SeekDB;
export { SeekDB };
