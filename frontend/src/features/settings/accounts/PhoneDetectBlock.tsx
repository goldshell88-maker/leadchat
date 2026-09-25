import { useEffect, useState } from "react";
import { Button, SegmentedControl, Skeleton, Switch, Text, Textarea } from "@mantine/core";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/shared/api/http";
import { showToast } from "@/shared/ui/toast";
import {
  fetchPhoneDetect,
  phoneDetectKey,
  savePhoneDetect,
  type MergeAutoMode,
  type PhoneDetectSettings,
} from "./api";

const MERGE_AUTO_HINT: Record<MergeAutoMode, string> = {
  off: "Двойники остаются подсказкой «Возможно, это тот же человек» — объединяет оператор",
  shadow:
    "Ничего не объединяется: в журнал пишется, какие пары склеились бы. По этим строкам решают, включать ли",
  on: "Склеиваются только пары, где обе карточки сами назвали один номер на разных наших аккаунтах, имена совпадают точь-в-точь и пару не разъединяли. Обратимо кнопкой «Разъединить»; два отката за сутки — и объединение само уходит в «только считать»",
};

/**
 * «Телефоны в переписке» — два переключателя разбора номеров во входящих.
 *
 * ЗАЧЕМ ОНИ НА ЭКРАНЕ. Номер, написанный внутри обычной фразы («звоните
 * 8 926 000-11-22»), до 12 августа не замечался вовсе: готовый контакт лежал в
 * переписке, а карточка клиента показывала кнопку «указать телефон». Разбор
 * появился — но он трогает ЧУЖИЕ данные, решая за клиента, что вот эта
 * цепочка цифр и есть его телефон. У владельца обязана быть возможность
 * остановить это одним щелчком, не дожидаясь выкатки.
 *
 * ВТОРОЙ ПЕРЕКЛЮЧАТЕЛЬ ОПАСНЕЕ ПЕРВОГО, и подписи это говорят прямо. Разбор
 * ошибается на цифрах, которые телефоном не являются (код домофона, артикул,
 * номер квартиры), а номер из карточки набирают и диктуют мастеру вслух:
 * ошибка стоит звонка постороннему человеку и потерянного настоящего клиента.
 * Поэтому по умолчанию на сервере автозапись выключена, и включают её осознанно.
 *
 * ПРАВО — `settings:manage`, то есть администратор. Руководителю блок не
 * показывается вовсе: сервер ему всё равно откажет, а погашенные тумблеры без
 * объяснения читаются как поломка.
 */
export function PhoneDetectBlock() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: phoneDetectKey, queryFn: fetchPhoneDetect });
  // Черновик списка «наши номера»: сохраняется по уходу из поля, а не на
  // каждую букву — иначе каждая цифра уезжала бы на сервер отдельной правкой.
  const [ownDraft, setOwnDraft] = useState<string | null>(null);
  const [ownError, setOwnError] = useState<string | null>(null);
  useEffect(() => {
    setOwnDraft(null);
  }, [q.data?.own_numbers]);

  /*
   * СОХРАНЕНИЕ СРАЗУ, БЕЗ КНОПКИ «Сохранить».
   *
   * На соседнем экране «Распределение» кнопка есть, и это не разнобой: там
   * форма из трёх связанных полей, и промежуточное состояние набора («потолок
   * стёрт, чтобы напечатать заново») сохранять нельзя. Здесь же каждый тумблер
   * — законченное решение, а всё остальное на этой странице применяется
   * немедленно: переименование, отключение, удаление канала. Тумблер, который
   * ждёт кнопки среди немедленных действий, читается как уже сохранённый.
   *
   * Цена ошибочного щелчка при этом названа в подписи, а сам факт смены с
   * именем и временем ложится в журнал аудита (`settings.phone_detect_changed`).
   */
  const save = useMutation({
    mutationFn: savePhoneDetect,
    onSuccess: (data) => {
      qc.setQueryData(phoneDetectKey, data);
      showToast({ title: "Сохранено", color: "lp" });
    },
    onError: (e) =>
      showToast({
        title: "Настройка не сохранилась",
        // Состояние тумблера берётся из ответа сервера, а не из своей памяти,
        // поэтому после неудачи он остаётся в прежнем положении сам. Отказ
        // сервера — его словами (проверка 24.09): «Проверьте соединение» на
        // «Слишком длинно» отправляло чинить не то.
        message:
          e instanceof ApiError ? e.message : "Проверьте соединение и переключите ещё раз",
        color: "red",
      }),
  });

  if (q.isPending) {
    return (
      <section className="accounts-page__block">
        <Skeleton height={18} width={220} radius="var(--lc-radius-sm)" />
        <Skeleton height={34} width={320} radius="var(--lc-radius-sm)" />
      </section>
    );
  }

  if (q.isError) {
    return (
      <section className="accounts-page__block">
        <Text fz="sm" c="var(--lc-danger-text)" role="alert">
          Настройки распознавания телефонов не загрузились. Сами настройки при этом не
          изменились — система работает так, как её настроили в прошлый раз
        </Text>
        <Button variant="outline" size="xs" onClick={() => void q.refetch()}>
          Повторить
        </Button>
      </section>
    );
  }

  const value: PhoneDetectSettings = q.data;
  // Ответ старого сервера в окне выкатки полей 12.09 не несёт — блок не
  // имеет права уронить весь экран настроек из-за одного списка.
  const разобрано: string[] = value.own_numbers_parsed ?? [];
  const режим: MergeAutoMode = value.merge_auto ?? "off";

  return (
    <section className="accounts-page__block" aria-labelledby="phone-detect-title">
      <Text id="phone-detect-title" fw={600} fz="md" c="var(--lc-text-1)">
        Телефоны в переписке
      </Text>

      <Switch
        checked={value.enabled}
        disabled={save.isPending}
        onChange={(e) => save.mutate({ enabled: e.currentTarget.checked })}
        label="Распознавать телефоны в тексте сообщений"
        // Подпись меняется вместе с тумблером: статичная продолжала бы
        // описывать выключенный режим после включения, и человек читал бы её
        // как состояние системы.
        description={
          value.enabled
            ? "Номер, написанный клиентом словами в сообщении, система находит сама"
            : "Номер из текста сообщения останется незамеченным — телефон вписывают руками"
        }
      />

      <Switch
        checked={value.autofill}
        // «Писать сразу» без «распознавать» — бессмыслица, и увидеть это надо
        // на экране, а не после сохранения.
        disabled={!value.enabled || save.isPending}
        onChange={(e) => save.mutate({ autofill: e.currentTarget.checked })}
        label="Писать найденный номер прямо в карточку"
        description={
          value.autofill
            ? "Пустой основной заполнится сам, остальные номера из переписки добавятся вторыми без вопроса — с датой и словом рядом («жена», «мастер»). Ошибка разбора (код домофона, артикул) станет номером в карточке, по которому позвонят; лишний снимается кнопкой «Не его номер»"
            : "Оператор увидит предложение «заменить», «добавить» или «отклонить» и решит сам"
        }
      />

      <Text fz="xs" c="var(--lc-text-3)">
        Телефон, уже указанный в карточке, распознавание не перезаписывает никогда —
        ни при каком сочетании этих двух переключателей. Похожий номер (одна цифра)
        помечается «возможно, исправление», а решает человек одним нажатием.
      </Text>

      <div className="accounts-page__field">
        <Textarea
          label="Наши номера"
          description="Номера объявлений, офиса, подписи бота — через запятую. Клиент пишет их, пересказывая, куда звонил; в карточку клиента они не попадут"
          size="xs"
          autosize
          minRows={1}
          value={ownDraft ?? value.own_numbers ?? ""}
          // Предел сервера (`PhoneDetectPatch.own_numbers`): лишний знак не
          // набирается, а не отвергается после ухода из поля.
          maxLength={2000}
          error={ownError}
          disabled={save.isPending}
          onChange={(e) => {
            setOwnDraft(e.currentTarget.value);
            setOwnError(null);
          }}
          onBlur={() => {
            if (ownDraft !== null && ownDraft.trim() !== value.own_numbers) {
              save.mutate(
                { own_numbers: ownDraft },
                // Несохранённый текст остаётся в поле — подсвечиваем его, чтобы
                // он не читался сохранённым.
                { onError: (e) => setOwnError(e instanceof ApiError ? e.message : "Не сохранилось") },
              );
            }
          }}
        />
        <Text fz="xs" c="var(--lc-text-3)" data-testid="own-numbers-parsed">
          {разобрано.length === 0 ? "Список пуст" : `Разобрано номеров: ${разобрано.length}`}
        </Text>
      </div>

      <div className="accounts-page__field">
        <Text fz="sm" c="var(--lc-text-2)" id="merge-auto-label">
          Объединять карточки-двойники по телефону
        </Text>
        <SegmentedControl
          aria-labelledby="merge-auto-label"
          size="xs"
          value={режим}
          disabled={save.isPending}
          onChange={(v) => save.mutate({ merge_auto: v as MergeAutoMode })}
          data={[
            { value: "off", label: "Нет" },
            { value: "shadow", label: "Только считать" },
            { value: "on", label: "Объединять" },
          ]}
        />
        <Text fz="xs" c="var(--lc-text-3)">
          {MERGE_AUTO_HINT[режим]}
        </Text>
      </div>
    </section>
  );
}
