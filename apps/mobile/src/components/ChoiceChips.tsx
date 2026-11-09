import { Pressable, ScrollView, StyleSheet, Text } from "react-native";
import { colors, spacing } from "../theme";

interface Choice {
  id: string;
  label: string;
}

interface Props {
  accessibilityLabel: string;
  choices: Choice[];
  selectedId?: string;
  onSelect: (id: string) => void;
  allowClear?: boolean;
}

export const ChoiceChips = ({
  accessibilityLabel,
  choices,
  selectedId,
  onSelect,
  allowClear = false,
}: Props) => (
  <ScrollView
    horizontal
    showsHorizontalScrollIndicator={false}
    contentContainerStyle={styles.row}
    accessibilityLabel={accessibilityLabel}
  >
    {allowClear ? (
      <Chip label="不关联" selected={!selectedId} onPress={() => onSelect("")} />
    ) : null}
    {choices.map((choice) => (
      <Chip
        key={choice.id}
        label={choice.label}
        selected={choice.id === selectedId}
        onPress={() => onSelect(choice.id)}
      />
    ))}
  </ScrollView>
);

const Chip = ({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) => (
  <Pressable
    accessibilityRole="radio"
    accessibilityState={{ selected }}
    onPress={onPress}
    style={({ pressed }) => [styles.chip, selected && styles.selected, pressed && styles.pressed]}
  >
    <Text style={[styles.label, selected && styles.selectedLabel]}>{label}</Text>
  </Pressable>
);

const styles = StyleSheet.create({
  row: { gap: spacing.sm, paddingVertical: 2 },
  chip: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 999,
    backgroundColor: colors.surface,
    paddingHorizontal: 14,
    paddingVertical: 9,
  },
  selected: { borderColor: colors.primary, backgroundColor: colors.primarySoft },
  label: { color: colors.textMuted, fontWeight: "600" },
  selectedLabel: { color: colors.primary },
  pressed: { opacity: 0.7 },
});
