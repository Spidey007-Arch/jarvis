import { useSyncExternalStore } from "react";

/** The interface language (Settings > General > Language). English text is the key; a missing Hindi line shows English. */
export type Lang = "en" | "hi";
export const LANGS: { id: Lang; label: string }[] = [
  { id: "en", label: "English" },
  { id: "hi", label: "हिन्दी (Hindi)" },
];

let current: Lang = "en";
const listeners = new Set<() => void>();

export function setLang(lang: Lang | string | undefined) {
  const next: Lang = lang === "hi" ? "hi" : "en";
  if (next === current) return;
  current = next;
  document.documentElement.lang = next;
  listeners.forEach((l) => l());
}

/** Re-renders the caller when the language changes. */
export function useLang(): Lang {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => current,
  );
}

export function t(text: string): string {
  return current === "hi" ? HI[text] ?? text : text;
}

const HI: Record<string, string> = {
  // panels
  Conversations: "बातचीत",
  Memory: "याददाश्त",
  Customize: "कस्टमाइज़",
  System: "सिस्टम",
  Settings: "सेटिंग्स",
  "New conversation": "नई बातचीत",
  "Close panel": "पैनल बंद करें",
  "Ask JARVIS…": "JARVIS से पूछें…",
  "No AI provider connected": "कोई AI प्रदाता जुड़ा नहीं है",
  // settings
  General: "सामान्य",
  "AI & models": "AI और मॉडल",
  Voice: "आवाज़",
  "PC control & safety": "PC नियंत्रण और सुरक्षा",
  "How JARVIS addresses you": "JARVIS आपको कैसे बुलाए",
  "Your name": "आपका नाम",
  "Form of address": "संबोधन",
  Language: "भाषा",
  "The interface, JARVIS's replies and the offline voice.": "इंटरफ़ेस, JARVIS के जवाब और ऑफ़लाइन आवाज़।",
  "Backup & sync": "बैकअप और सिंक",
  SAVE: "सेव करें",
  CANCEL: "रद्द करें",
  DELETE: "हटाएँ",
  EDIT: "बदलें",
  On: "चालू",
  Off: "बंद",
  // customize
  Instructions: "निर्देश",
  Skills: "स्किल्स",
  Automations: "ऑटोमेशन",
  Connectors: "कनेक्टर",
  "API keys": "API कुंजियाँ",
  Calendar: "कैलेंडर",
  Heartbeat: "हार्टबीट",
  Plugins: "प्लगइन",
  // calendar
  "Coming up": "आगे क्या है",
  "Nothing in the next two weeks.": "अगले दो हफ़्तों में कुछ नहीं।",
  "+ NEW EVENT": "+ नया इवेंट",
  Title: "शीर्षक",
  Starts: "शुरू",
  Ends: "खत्म",
  "All day": "पूरे दिन",
  Where: "कहाँ",
  "Add to": "किसमें जोड़ें",
  "JARVIS's calendar": "JARVIS का कैलेंडर",
  "Outlook on this PC": "इस PC का Outlook",
  "Google Calendar (opens it)": "Google Calendar (खोलता है)",
  "Outlook on the web (opens it)": "वेब पर Outlook (खोलता है)",
  "Remind me": "मुझे याद दिलाएँ",
  "Linked calendars": "जुड़े कैलेंडर",
  Name: "नाम",
  "Private address (iCal)": "निजी पता (iCal)",
  LINK: "जोड़ें",
  REMOVE: "हटाएँ",
  "Also use classic Outlook on this PC": "इस PC के क्लासिक Outlook का भी इस्तेमाल करें",
  // heartbeat
  "What to check": "क्या जाँचना है",
  "Every (minutes)": "हर (मिनट)",
  "Quiet from": "शांत समय से",
  to: "तक",
  "CHECK NOW": "अभी जाँचें",
  "Last check": "पिछली जाँच",
  "Recently said": "हाल में कहा",
  "Heartbeat is on": "हार्टबीट चालू है",
  // plugins
  Installed: "इंस्टॉल किए हुए",
  "Community catalog": "कम्युनिटी कैटलॉग",
  "Install from a file": "फ़ाइल से इंस्टॉल करें",
  VIEW: "देखें",
  INSTALL: "इंस्टॉल करें",
  UPDATE: "अपडेट करें",
  "SHARE MY SKILLS": "मेरी स्किल्स शेयर करें",
  // backup
  "Back up now": "अभी बैकअप लें",
  Restore: "रिस्टोर करें",
  Passphrase: "पासफ़्रेज़",
  "Sync folder": "सिंक फ़ोल्डर",
  "Sync between my PCs": "मेरे PCs के बीच सिंक करें",
  "SYNC NOW": "अभी सिंक करें",
  "Include API keys and tokens": "API कुंजियाँ और टोकन भी शामिल करें",
};
