/**
 * MirrAI Common Validator
 * Handles validation for personal information and other forms.
 */
const Validator = {
    // Regex Patterns
    patterns: {
        name: /^[가-힣a-zA-Z]{2,10}$/, // Korean or English, 2-10 chars, no whitespace
        phone: /^010-\d{4}-\d{4}$/,      // 010-XXXX-XXXX
        phoneRaw: /^010\d{8}$/,          // 010XXXXXXXX
        id: /^[a-zA-Z0-9_]{4,20}$/,      // Alphanumeric, 4-20 chars
        brn: /^\d{3}-\d{2}-\d{5}$/       // XXX-XX-XXXXX (Business Registration Number)
    },

    // Validation Functions
    isValidName(name) {
        return this.patterns.name.test(name.trim());
    },

    isValidPhone(phone) {
        const cleaned = phone.replace(/\s/g, '');
        return this.patterns.phone.test(cleaned) || this.patterns.phoneRaw.test(cleaned);
    },

    isValidId(id) {
        return this.patterns.id.test(id.trim());
    },

    isValidBRN(brn) {
        return this.patterns.brn.test(brn.trim());
    },

    isValidPassword(pw) {
        return pw.length >= 8;
    },

    // Format Helpers
    formatPhone(value) {
        const cleaned = value.replace(/\D/g, '');
        if (cleaned.length <= 3) return cleaned;
        if (cleaned.length <= 7) return `${cleaned.slice(0, 3)}-${cleaned.slice(3)}`;
        return `${cleaned.slice(0, 3)}-${cleaned.slice(3, 7)}-${cleaned.slice(7, 11)}`;
    },

    formatBRN(value) {
        const cleaned = value.replace(/\D/g, '');
        if (cleaned.length <= 3) return cleaned;
        if (cleaned.length <= 5) return `${cleaned.slice(0, 3)}-${cleaned.slice(3)}`;
        return `${cleaned.slice(0, 3)}-${cleaned.slice(3, 5)}-${cleaned.slice(5, 10)}`;
    }
};

window.MirrAIValidator = Validator;
