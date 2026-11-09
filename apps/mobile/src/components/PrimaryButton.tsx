import { ActivityIndicator, Pressable, StyleSheet, Text } from "react-native";
import { colors, spacing } from "../theme";

interface Props {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  busy?: boolean;
  variant?: "primary" | "secondary";
}

export const PrimaryButton = ({
  label,
  onPress,
  disabled = false,
  busy = false,
  variant = "primary",
}: Props) => (
  <Pressable
    accessibilityRole="button"
    accessibilityState={{ disabled: disabled || busy, busy }}
    disabled={disabled || busy}
    onPress={onPress}
    style={({ pressed }) => [
      styles.button,
      variant === "secondary" && styles.secondary,
      (disabled || busy) && styles.disabled,
      pressed && styles.pressed,
    ]}
  >
    {busy ? (
      <ActivityIndicator color={variant === "primary" ? "#FFFFFF" : colors.primary} />
    ) : (
      <Text style={[styles.label, variant === "secondary" && styles.secondaryLabel]}>{label}</Text>
    )}
  </Pressable>
);

const styles = StyleSheet.create({
  button: {
    minHeight: 50,
    paddingHorizontal: spacing.lg,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.primary,
  },
  secondary: { backgroundColor: colors.primarySoft },
  label: { color: "#FFFFFF", fontSize: 16, fontWeight: "700" },
  secondaryLabel: { color: colors.primary },
  disabled: { opacity: 0.48 },
  pressed: { opacity: 0.76 },
});
