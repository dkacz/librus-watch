"""Wymyślone odpowiedzi JSON-owego API o kształtach z rozpoznania (librus-apix-notes.md).

Wartości są fikcyjne. Kształt (klucze główne, typy, nazwy pól) odpowiada
odpowiedziom z prawdziwych kont rodzica.
"""

from __future__ import annotations

from typing import Any

LESSON = {
    "Lesson": {"Id": "41999"},
    "Class": {"Id": 7},
    "Subject": {"Id": "501"},
    "Teacher": {"Id": "1001"},
    "LessonNo": "1",
    "HourFrom": "08:00",
    "HourTo": "08:45",
    "IsCanceled": False,
    "IsSubstitutionClass": False,
    "SubstitutionNote": None,
    "DateFrom": "2026-09-01",
    "DateTo": "2027-06-26",
    "DayNo": "1",
    "TimetableEntry": {"Id": 9001},
}


def _timetable(monday: str) -> dict[str, Any]:
    return {
        "Timetable": {
            monday: [[], [LESSON], [{**LESSON, "LessonNo": "2"}, {**LESSON, "LessonNo": "2"}]],
            "2026-11-10": [[], [{**LESSON, "IsCanceled": True}]],
        },
        "Pages": {"Next": "x", "Prev": "y"},
    }


PAYLOADS: dict[str, dict[str, Any]] = {
    "Me": {"Me": {"Account": {"Id": 1}, "User": {"Id": 2}, "Class": {"Id": 7}, "Refresh": 1}},
    "Classes": {"Class": {"Id": 7, "Number": 3, "Symbol": "b", "Unit": {"Id": 1}}},
    "TimetableEntries": {
        "TimetableEntries": [
            {
                "Id": 9001,
                "Lesson": {"Id": "41999"},
                "DayOfTheWeek": "1",
                "LessonNo": "1",
                "DateFrom": "2026-09-01",
                "DateTo": "2027-06-26",
            }
        ]
    },
    "Calendars/Substitutions": {
        "Substitutions": [
            {
                "Id": 1,
                "OrgDate": "2026-11-12",
                "OrgLessonNo": "3",
                "OrgSubject": {"Id": "501"},
                "OrgTeacher": {"Id": "1001"},
                "IsCancelled": True,
                "IsShifted": False,
            },
            {
                "Id": 2,
                "OrgDate": "2026-11-13",
                "OrgLessonNo": "1",
                "IsCancelled": False,
                "IsShifted": False,
                "Date": "2026-11-13",
                "LessonNo": "1",
                "Subject": {"Id": "502"},
                "Teacher": {"Id": "1002"},
            },
        ]
    },
    "ClassFreeDays": {
        "ClassFreeDays": [
            {
                "Id": 5,
                "Class": {"Id": 7},
                "Type": {"Id": 3},
                "DateFrom": "2026-11-20",
                "DateTo": "2026-11-20",
                "LessonNoFrom": "4",
                "LessonNoTo": "6",
            },
            {
                "Id": 6,
                "Class": {"Id": 99},
                "Type": {"Id": 3},
                "DateFrom": "2026-11-21",
                "DateTo": "2026-11-21",
            },
        ]
    },
    "SchoolFreeDays": {
        "SchoolFreeDays": [
            {
                "Id": 1,
                "Name": "Dzień wolny",
                "DateFrom": "2026-11-02",
                "DateTo": "2026-11-02",
                "Units": [{"Id": 1}],
            }
        ]
    },
    "HomeWorks": {
        "HomeWorks": [
            {
                "Id": 11,
                "Category": {"Id": 4},
                "Class": {"Id": 7},
                "Subject": {"Id": "501"},
                "Content": "Wycieczka do muzeum. Zbiórka 7:30 pod szkołą.",
                "Date": "2026-11-18",
                "TimeFrom": "07:30:00",
                "TimeTo": "12:00:00",
                "LessonNo": None,
                "CreatedBy": {"Id": "1001"},
                "AddDate": "2026-11-05 10:00:00",
            }
        ]
    },
    "HomeWorks/Categories": {"Categories": [{"Id": 4, "Name": "Wycieczka", "Color": {"Id": 1}}]},
    "HomeWorkAssignments": {
        "HomeWorkAssignments": [
            {
                "Id": 21,
                "Topic": "Zeszyt",
                "Text": "Proszę przynieść podpisaną zgodę.",
                "Teacher": {"Id": "1002"},
                "Date": "2026-11-05",
                "DueDate": "2026-11-23",
                "Lesson": {"Id": "41999"},
                "Student": {"Id": 2},
                "MustSendAttachFile": False,
                "SendFilePossible": False,
                "AddedFiles": 0,
                "HomeworkAssigmentFiles": [],
            }
        ]
    },
    "SchoolNotices": {
        "SchoolNotices": [
            {
                "Id": "LID-NBOARD-NOTICE-1",
                "Subject": "Strój galowy",
                "Content": "W piątek apel.",
                "StartDate": "2026-11-05",
                "EndDate": "2026-11-14",
                "CreationDate": "2026-11-05 08:00:00",
                "AddedBy": {"Id": "1001"},
                "WasRead": False,
            }
        ]
    },
    "Notes": {"Notes": []},
    "Notes/Categories": {"Categories": []},
    "Grades": {
        "Grades": [
            {
                "Id": 31,
                "Grade": "5",
                "Subject": {"Id": "501"},
                "Comments": [{"Id": 41}],
                "Date": "2026-11-04",
                "AddDate": "2026-11-04 12:00:00",
            }
        ]
    },
    "Grades/Comments": {
        "Comments": [
            {
                "Id": 41,
                "Grade": {"Id": 31},
                "AddedBy": {"Id": "1001"},
                "Text": "Poprawa w czwartek na 1. lekcji.",
            }
        ]
    },
    "Realizations": {"Realizations": []},
    "ParentTeacherConferences": {
        "ParentTeacherConferences": [
            {
                "Id": 51,
                "Name": "Zebranie",
                "Topic": "Wyniki",
                "Date": "2026-11-19",
                "Time": "17:00:00",
                "Teacher": {"Id": "1001"},
                "Class": {"Id": 7},
            }
        ]
    },
    "SchoolTrips": {
        "Data": [
            {
                "id": 61,
                "destination": "Muzeum",
                "route": "",
                "locomotion": "autokar",
                "termFrom": "2026-11-18",
                "termTo": "2026-11-18",
            }
        ]
    },
    "Subjects": {"Subjects": [{"Id": 501, "Name": "Matematyka", "Short": "mat"}]},
    "Users": {
        "Users": [{"Id": 1001, "FirstName": "Anna", "LastName": "Testowa", "IsEmployee": True}],
        "pagination": {"limit": 1, "page": 1, "total": 1},
    },
}


def payload(path: str) -> dict[str, Any]:
    """Odpowiedź dla ścieżki API (z parametrami albo bez).

    Args:
        path: Ścieżka względem adresu API.

    Returns:
        Wymyślona odpowiedź.

    Raises:
        KeyError: Gdy dla ścieżki nie ma danych.
    """
    name, _, query = path.partition("?")
    if name == "Timetables":
        return _timetable(query.removeprefix("weekStart="))
    if name == "Timetables/OtherActivitiesRegister":
        return {
            "data": [
                {
                    "identifier": "LID-OAR-1",
                    "status": "CANCELED",
                    "date": "2026-11-12",
                    "startTime": "14:00",
                    "endTime": "15:00",
                    "title": "Chór",
                }
            ]
        }
    return PAYLOADS[name]
