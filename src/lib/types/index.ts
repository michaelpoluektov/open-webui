export type Banner = {
	id: string;
	type: string;
	title?: string;
	content: string;
	url?: string;
	dismissible?: boolean;
	timestamp: number;
};

export enum TTS_RESPONSE_SPLIT {
	PUNCTUATION = 'punctuation',
	PARAGRAPHS = 'paragraphs',
	NONE = 'none'
}

export interface Model {
	id: string;
	name: string;
	info?: {
		meta?: {
			capabilities?: {
				usage?: boolean;
			};
		};
	};
}
