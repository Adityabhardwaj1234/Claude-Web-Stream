export interface StreamChunk {
    id?: string;
    choices?: any[];
    usage?: any;
}
export declare class Client {
    baseUrl: string;
    apiKey: string;
    constructor(baseUrl?: string, apiKey?: string);
    chatCompletions(params: any): AsyncGenerator<StreamChunk>;
}
