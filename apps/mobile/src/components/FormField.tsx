import { StyleSheet, Text, TextInput, View } from "react-native";
import type { TextInputProps } from "react-native";
import { colors, spacing } from "../theme";

interface Props extends TextInputProps {
  label: string;
  hint?: string;
}

export const FormField = ({ label, hint, multiline, ...inputProps }: Props) => (
  <View style={styles.group}>
    <Text style={styles.label}>{label}</Text>
    {hint ? <Text style={styles.hint}>{hint}</Text> : null}
    <TextInput
      {...inputProps}
      multiline={multiline}
      placeholderTextColor="#90968F"
      style={[styles.input, multiline && styles.multiline]}
      textAlignVertical={multiline ? "top" : "center"}
    />
  </View>
);

const styles = StyleSheet.create({
  group: { gap: spacing.xs },
  label: { fontSize: 15, fontWeight: "700", color: colors.text },
  hint: { color: colors.textMuted, fontSize: 13, lineHeight: 18 },
  input: {
    minHeight: 48,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    backgroundColor: colors.surface,
    color: colors.text,
    fontSize: 16,
    paddingHorizontal: 14,
    paddingVertical: 12,
  },
  multiline: { minHeight: 92 },
});
