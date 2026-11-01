export interface StreamChunk { id?: string; choices?: any[]; usage?: any; }

export class Client {
  constructor(public baseUrl = 'http://127.0.0.1:8787', public apiKey = '') {}
  async *chatCompletions(params: any): AsyncGenerator<StreamChunk> {
    const res = await fetch(this.baseUrl + '/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + this.apiKey },
      body: JSON.stringify(params),
    });
    if (!res.body) throw new Error('no body');
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() || '';
      for (const p of parts) {
        for (const line of p.split('\n')) {
          if (line.startsWith('data: ')) {
            const d = line.slice(6);
            if (d === '[DONE]') return;
            yield JSON.parse(d);
          }
        }
      }
    }
  }
}